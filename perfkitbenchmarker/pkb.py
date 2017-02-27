# Copyright 2014 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Runs all benchmarks in PerfKitBenchmarker.

All benchmarks in PerfKitBenchmarker export the following interface:

GetConfig: this returns, the name of the benchmark, the number of machines
         required to run one instance of the benchmark, a detailed description
         of the benchmark, and if the benchmark requires a scratch disk.
Prepare: this function takes a list of VMs as an input parameter. The benchmark
         will then get all binaries required to run the benchmark and, if
         required, create data files.
Run: this function takes a list of VMs as an input parameter. The benchmark will
     then run the benchmark upon the machines specified. The function will
     return a dictonary containing the results of the benchmark.
Cleanup: this function takes a list of VMs as an input parameter. The benchmark
         will then return the machine to the state it was at before Prepare
         was called.

PerfKitBenchmarker has the following run stages: provision, prepare,
    run, cleanup, teardown, and all.

provision: Read command-line flags, decide what benchmarks to run, and
    create the necessary resources for each benchmark, including
    networks, VMs, disks, and keys, and generate a run_uri, which can
    be used to resume execution at later stages.
prepare: Execute the Prepare function of each benchmark to install
         necessary software, upload datafiles, etc.
run: Execute the Run function of each benchmark and collect the
     generated samples. The publisher may publish these samples
     according to PKB's settings. The Run stage can be called multiple
     times with the run_uri generated by the provision stage.
cleanup: Execute the Cleanup function of each benchmark to uninstall
         software and delete data files.
teardown: Delete VMs, key files, networks, and disks created in the
    'provision' stage.

all: PerfKitBenchmarker will run all of the above stages (provision,
     prepare, run, cleanup, teardown). Any resources generated in the
     provision stage will be automatically deleted in the teardown
     stage, even if there is an error in an earlier stage. When PKB is
     running in this mode, the run cannot be repeated or resumed using
     the run_uri.
"""

import collections
import getpass
import itertools
import logging
import multiprocessing
import re
import sys
import time
import uuid

from perfkitbenchmarker import archive
from perfkitbenchmarker import background_tasks
from perfkitbenchmarker import benchmark_sets
from perfkitbenchmarker import benchmark_spec
from perfkitbenchmarker import benchmark_status
from perfkitbenchmarker import configs
from perfkitbenchmarker import context
from perfkitbenchmarker import disk
from perfkitbenchmarker import errors
from perfkitbenchmarker import events
from perfkitbenchmarker import flags
from perfkitbenchmarker import flag_util
from perfkitbenchmarker import linux_benchmarks
from perfkitbenchmarker import log_util
from perfkitbenchmarker import os_types
from perfkitbenchmarker import requirements
from perfkitbenchmarker import spark_service
from perfkitbenchmarker import stages
from perfkitbenchmarker import static_virtual_machine
from perfkitbenchmarker import timing_util
from perfkitbenchmarker import traces
from perfkitbenchmarker import version
from perfkitbenchmarker import vm_util
from perfkitbenchmarker import windows_benchmarks
from perfkitbenchmarker import abort
from perfkitbenchmarker.configs import benchmark_config_spec
from perfkitbenchmarker.linux_benchmarks import cluster_boot_benchmark
from perfkitbenchmarker.publisher import SampleCollector

LOG_FILE_NAME = 'pkb.log'
REQUIRED_INFO = ['scratch_disk', 'num_machines']
REQUIRED_EXECUTABLES = frozenset(['ssh', 'ssh-keygen', 'scp', 'openssl'])
FLAGS = flags.FLAGS

flags.DEFINE_list('ssh_options', [], 'Additional options to pass to ssh.')
flags.DEFINE_list('benchmarks', [benchmark_sets.STANDARD_SET],
                  'Benchmarks and/or benchmark sets that should be run. The '
                  'default is the standard set. For more information about '
                  'benchmarks and benchmark sets, see the README and '
                  'benchmark_sets.py.')
flags.DEFINE_string('archive_bucket', None,
                    'Archive results to the given S3/GCS bucket.')
flags.DEFINE_string('project', None, 'GCP project ID under which '
                    'to create the virtual machines')
flags.DEFINE_list(
    'zones', [],
    'A list of zones within which to run PerfKitBenchmarker. '
    'This is specific to the cloud provider you are running on. '
    'If multiple zones are given, PerfKitBenchmarker will create 1 VM in '
    'zone, until enough VMs are created as specified in each '
    'benchmark. The order in which this flag is applied to VMs is '
    'undefined.')
flags.DEFINE_list(
    'extra_zones', [],
    'Zones that will be appended to the "zones" list. This is functionally '
    'the same, but allows flag matrices to have two zone axes.')
# TODO(user): note that this is currently very GCE specific. Need to create a
#    module which can translate from some generic types to provider specific
#    nomenclature.
flags.DEFINE_string('machine_type', None, 'Machine '
                    'types that will be created for benchmarks that don\'t '
                    'require a particular type.')
flags.DEFINE_integer('num_vms', 1, 'For benchmarks which can make use of a '
                     'variable number of machines, the number of VMs to use.')
flags.DEFINE_string('image', None, 'Default image that will be '
                    'linked to the VM')
flags.DEFINE_string('run_uri', None, 'Name of the Run. If provided, this '
                    'should be alphanumeric and less than or equal to 10 '
                    'characters in length.')
flags.DEFINE_string('owner', getpass.getuser(), 'Owner name. '
                    'Used to tag created resources and performance records.')
flags.DEFINE_enum(
    'log_level', log_util.INFO,
    log_util.LOG_LEVELS.keys(),
    'The log level to run at.')
flags.DEFINE_enum(
    'file_log_level', log_util.DEBUG, log_util.LOG_LEVELS.keys(),
    'Anything logged at this level or higher will be written to the log file.')
flags.DEFINE_integer('duration_in_seconds', None,
                     'duration of benchmarks. '
                     '(only valid for mesh_benchmark)')
flags.DEFINE_string('static_vm_file', None,
                    'The file path for the Static Machine file. See '
                    'static_virtual_machine.py for a description of this file.')
flags.DEFINE_boolean('version', False, 'Display the version and exit.')
flags.DEFINE_enum(
    'scratch_disk_type', None,
    [disk.STANDARD, disk.REMOTE_SSD, disk.PIOPS, disk.LOCAL],
    'Type for all scratch disks. The default is standard')
flags.DEFINE_string(
    'data_disk_type', None,
    'Type for all data disks. If a provider keeps the operating system and '
    'user data on separate disks, this only affects the user data disk(s).'
    'If the provider has OS and user data on the same disk, this flag affects'
    'that disk.')
flags.DEFINE_integer('scratch_disk_size', None, 'Size, in gb, for all scratch '
                     'disks.')
flags.DEFINE_integer('data_disk_size', None, 'Size, in gb, for all data disks.')
flags.DEFINE_integer('scratch_disk_iops', None,
                     'IOPS for Provisioned IOPS (SSD) volumes in AWS.')
flags.DEFINE_integer('num_striped_disks', None,
                     'The number of data disks to stripe together to form one '
                     '"logical" data disk. This defaults to 1 '
                     '(except with local disks), which means no striping. '
                     'When using local disks, they default to striping '
                     'all disks together. The striped disks will appear as '
                     'one disk (data_disk_0) in the metadata.',
                     lower_bound=1)
flags.DEFINE_bool('install_packages', None,
                  'Override for determining whether packages should be '
                  'installed. If this is false, no packages will be installed '
                  'on any VMs. This option should probably only ever be used '
                  'if you have already created an image with all relevant '
                  'packages installed.')
flags.DEFINE_bool(
    'stop_after_benchmark_failure', False,
    'Determines response when running multiple benchmarks serially and a '
    'benchmark run fails. When True, no further benchmarks are scheduled, and '
    'execution ends. When False, benchmarks continue to be scheduled. Does not '
    'apply to keyboard interrupts, which will always prevent further '
    'benchmarks from being scheduled.')
flags.DEFINE_boolean(
    'ignore_package_requirements', False,
    'Disables Python package requirement runtime checks.')
flags.DEFINE_enum('spark_service_type', None,
                  [spark_service.PKB_MANAGED, spark_service.PROVIDER_MANAGED],
                  'Type of spark service to use')
flags.DEFINE_boolean(
    'publish_after_run', False,
    'If true, PKB will publish all samples available immediately after running '
    'each benchmark. This may be useful in scenarios where the PKB run time '
    'for all benchmarks is much greater than a single benchmark.')
flags.DEFINE_integer(
    'run_stage_time', 0,
    'PKB will run/re-run the run stage of each benchmark until it has spent '
    'at least this many seconds. It defaults to 0, so benchmarks will only '
    'be run once unless some other value is specified.')
flags.DEFINE_integer(
    'run_stage_retries', 0,
    'The number of allowable consecutive failures during the run stage. After '
    'this number of failures any exceptions will cause benchmark termination. '
    'If run_stage_time is exceeded, the run stage will not be retried even if '
    'the number of failures is less than the value of this flag.')
flags.DEFINE_boolean(
    'boot_samples', False,
    'Whether to publish boot time samples for all tests.')
flags.DEFINE_integer(
    'run_processes', 1,
    'The number of parallel processes to use to run benchmarks.',
    lower_bound=1)
flags.DEFINE_string(
    'helpmatch', '',
    'Shows only flags defined in a module whose name matches the given regex.')

# Support for using a proxy in the cloud environment.
flags.DEFINE_string('http_proxy', '',
                    'Specify a proxy for HTTP in the form '
                    '[user:passwd@]proxy.server:port.')
flags.DEFINE_string('https_proxy', '',
                    'Specify a proxy for HTTPS in the form '
                    '[user:passwd@]proxy.server:port.')
flags.DEFINE_string('ftp_proxy', '',
                    'Specify a proxy for FTP in the form '
                    '[user:passwd@]proxy.server:port.')

MAX_RUN_URI_LENGTH = 8

_TEARDOWN_EVENT = multiprocessing.Event()

events.initialization_complete.connect(traces.RegisterAll)


def _InjectBenchmarkInfoIntoDocumentation():
  """Appends each benchmark's information to the main module's docstring."""
  # TODO: Verify if there is other way of appending additional help
  # message.
  # Inject more help documentation
  # The following appends descriptions of the benchmarks and descriptions of
  # the benchmark sets to the help text.
  benchmark_sets_list = [
      '%s:  %s' %
      (set_name, benchmark_sets.BENCHMARK_SETS[set_name]['message'])
      for set_name in benchmark_sets.BENCHMARK_SETS]
  sys.modules['__main__'].__doc__ = (
      'PerfKitBenchmarker version: {version}\n\n{doc}\n'
      'Benchmarks (default requirements):\n'
      '\t{benchmark_doc}').format(
          version=version.VERSION,
          doc=__doc__,
          benchmark_doc=_GenerateBenchmarkDocumentation())
  sys.modules['__main__'].__doc__ += ('\n\nBenchmark Sets:\n\t%s'
                                      % '\n\t'.join(benchmark_sets_list))


def _ParseFlags(argv=sys.argv):
  """Parses the command-line flags."""
  try:
    argv = FLAGS(argv)
  except flags.FlagsError as e:
    logging.error(e)
    logging.info('For usage instructions, use --helpmatch={module_name}')
    logging.info('For example, ./pkb.py --helpmatch=benchmarks.fio')
    sys.exit(1)


def _PrintHelp(matches=None):
  """Prints help for flags defined in matching modules.

  Args:
    matches regex string or None. Filters help to only those whose name
      matched the regex. If None then all flags are printed.
  """
  if not matches:
    print FLAGS
  else:
    flags_by_module = FLAGS.FlagsByModuleDict()
    modules = sorted(flags_by_module)
    regex = re.compile(matches)
    for module_name in modules:
      if regex.search(module_name):
        print FLAGS.ModuleHelp(module_name)


def CheckVersionFlag():
  """If the --version flag was specified, prints the version and exits."""
  if FLAGS.version:
    print version.VERSION
    sys.exit(0)


def _InitializeRunUri():
  """Determines the PKB run URI and sets FLAGS.run_uri."""
  if FLAGS.run_uri is None:
    if stages.PROVISION in FLAGS.run_stage:
      FLAGS.run_uri = str(uuid.uuid4())[-8:]
    else:
      # Attempt to get the last modified run directory.
      run_uri = vm_util.GetLastRunUri()
      if run_uri:
        FLAGS.run_uri = run_uri
        logging.warning(
            'No run_uri specified. Attempting to run the following stages with '
            '--run_uri=%s: %s', FLAGS.run_uri, ', '.join(FLAGS.run_stage))
      else:
        raise errors.Setup.NoRunURIError(
            'No run_uri specified. Could not run the following stages: %s' %
            ', '.join(FLAGS.run_stage))
  elif not FLAGS.run_uri.isalnum() or len(FLAGS.run_uri) > MAX_RUN_URI_LENGTH:
    raise errors.Setup.BadRunURIError('run_uri must be alphanumeric and less '
                                      'than or equal to 8 characters in '
                                      'length.')


def _CreateBenchmarkSpecs():
  """Create a list of BenchmarkSpecs for each benchmark run to be scheduled.

  Returns:
    A list of BenchmarkSpecs.
  """
  specs = []
  benchmark_tuple_list = benchmark_sets.GetBenchmarksFromFlags()
  benchmark_counts = collections.defaultdict(itertools.count)
  for benchmark_module, user_config in benchmark_tuple_list:
    # Construct benchmark config object.
    name = benchmark_module.BENCHMARK_NAME
    expected_os_types = (
        os_types.WINDOWS_OS_TYPES if FLAGS.os_type in os_types.WINDOWS_OS_TYPES
        else os_types.LINUX_OS_TYPES)
    merged_flags = benchmark_config_spec.FlagsDecoder().Decode(
        user_config.get('flags'), 'flags', FLAGS)
    with flag_util.FlagDictSubstitution(FLAGS, lambda: merged_flags):
      config_dict = benchmark_module.GetConfig(user_config)
    config_spec_class = getattr(
        benchmark_module, 'BENCHMARK_CONFIG_SPEC_CLASS',
        benchmark_config_spec.BenchmarkConfigSpec)
    config = config_spec_class(name, expected_os_types=expected_os_types,
                               flag_values=FLAGS, **config_dict)

    # Assign a unique ID to each benchmark run. This differs even between two
    # runs of the same benchmark within a single PKB run.
    uid = name + str(benchmark_counts[name].next())

    # Optional step to check flag values and verify files exist.
    check_prereqs = getattr(benchmark_module, 'CheckPrerequisites', None)
    if check_prereqs:
      try:
        with config.RedirectFlags(FLAGS):
          check_prereqs(config)
      except:
        logging.exception('Prerequisite check failed for %s', name)
        raise

    specs.append(benchmark_spec.BenchmarkSpec.GetBenchmarkSpec(
        benchmark_module, config, uid))

  return specs


def DoProvisionPhase(spec, timer):
  """Performs the Provision phase of benchmark execution.

  Args:
    spec: The BenchmarkSpec created for the benchmark.
    timer: An IntervalTimer that measures the start and stop times of resource
      provisioning.
  """
  logging.info('Provisioning resources for benchmark %s', spec.name)
  # spark service needs to go first, because it adds some vms.
  spec.ConstructSparkService()
  spec.ConstructDpbService()
  spec.ConstructVirtualMachines()
  # Pickle the spec before we try to create anything so we can clean
  # everything up on a second run if something goes wrong.
  spec.Pickle()
  events.benchmark_start.send(benchmark_spec=spec)
  try:
    with timer.Measure('Resource Provisioning'):
      spec.Provision()
  finally:
    # Also pickle the spec after the resources are created so that
    # we have a record of things like AWS ids. Otherwise we won't
    # be able to clean them up on a subsequent run.
    spec.Pickle()


def DoPreparePhase(spec, timer):
  """Performs the Prepare phase of benchmark execution.

  Args:
    spec: The BenchmarkSpec created for the benchmark.
    timer: An IntervalTimer that measures the start and stop times of the
      benchmark module's Prepare function.
  """
  logging.info('Preparing benchmark %s', spec.name)
  with timer.Measure('BenchmarkSpec Prepare'):
    spec.Prepare()
  with timer.Measure('Benchmark Prepare'):
    spec.BenchmarkPrepare(spec)
  spec.StartBackgroundWorkload()


def DoRunPhase(spec, collector, timer):
  """Performs the Run phase of benchmark execution.

  Args:
    spec: The BenchmarkSpec created for the benchmark.
    collector: The SampleCollector object to add samples to.
    timer: An IntervalTimer that measures the start and stop times of the
      benchmark module's Run function.
  """
  deadline = time.time() + FLAGS.run_stage_time
  run_number = 0
  consecutive_failures = 0
  while True:
    samples = []
    logging.info('Running benchmark %s', spec.name)
    events.before_phase.send(events.RUN_PHASE, benchmark_spec=spec)
    try:
      with timer.Measure('Benchmark Run'):
        samples = spec.BenchmarkRun(spec)
      if (FLAGS.boot_samples or
          spec.name == cluster_boot_benchmark.BENCHMARK_NAME):
        samples.extend(cluster_boot_benchmark.GetTimeToBoot(spec.vms))
    except Exception:
      consecutive_failures += 1
      if consecutive_failures > FLAGS.run_stage_retries:
        raise
      logging.exception('Run failed (consecutive_failures=%s); retrying.',
                        consecutive_failures)
    else:
      consecutive_failures = 0
    finally:
      events.after_phase.send(events.RUN_PHASE, benchmark_spec=spec)
    events.samples_created.send(
        events.RUN_PHASE, benchmark_spec=spec, samples=samples)
    if FLAGS.run_stage_time:
      for sample in samples:
        sample.metadata['run_number'] = run_number
    collector.AddSamples(samples, spec.name, spec)
    if FLAGS.publish_after_run:
      collector.PublishSamples()
    run_number += 1
    if time.time() > deadline:
      break


def DoCleanupPhase(spec, timer):
  """Performs the Cleanup phase of benchmark execution.

  Args:
    spec: The BenchmarkSpec created for the benchmark.
    timer: An IntervalTimer that measures the start and stop times of the
      benchmark module's Cleanup function.
  """
  logging.info('Cleaning up benchmark %s', spec.name)

  if spec.always_call_cleanup or any([vm.is_static for vm in spec.vms]):
    spec.StopBackgroundWorkload()
    with timer.Measure('Benchmark Cleanup'):
      spec.BenchmarkCleanup(spec)


def DoTeardownPhase(spec, timer):
  """Performs the Teardown phase of benchmark execution.

  Args:
    name: A string containing the benchmark name.
    spec: The BenchmarkSpec created for the benchmark.
    timer: An IntervalTimer that measures the start and stop times of
      resource teardown.
  """
  logging.info('Tearing down resources for benchmark %s', spec.name)

  with timer.Measure('Resource Teardown'):
    spec.Delete()


def RunBenchmark(spec, collector):
  """Runs a single benchmark and adds the results to the collector.

  Args:
    spec: The BenchmarkSpec object with run information.
    collector: The SampleCollector object to add samples to.
  """
  spec.status = benchmark_status.FAILED
  # Modify the logger prompt for messages logged within this function.
  label_extension = '{}({}/{})'.format(
      spec.name, spec.sequence_number, spec.total_benchmarks)
  context.SetThreadBenchmarkSpec(spec)
  log_context = log_util.GetThreadLogContext()
  with log_context.ExtendLabel(label_extension):
    with spec.RedirectGlobalFlags():
      end_to_end_timer = timing_util.IntervalTimer()
      detailed_timer = timing_util.IntervalTimer()
      try:
        with end_to_end_timer.Measure('End to End'):
          if stages.PROVISION in FLAGS.run_stage:
            DoProvisionPhase(spec, detailed_timer)

          if stages.PREPARE in FLAGS.run_stage:
            DoPreparePhase(spec, detailed_timer)

          if stages.RUN in FLAGS.run_stage:
            DoRunPhase(spec, collector, detailed_timer)

          if stages.CLEANUP in FLAGS.run_stage:
            DoCleanupPhase(spec, detailed_timer)

          if stages.TEARDOWN in FLAGS.run_stage:
            DoTeardownPhase(spec, detailed_timer)

        # Add timing samples.
        if (FLAGS.run_stage == stages.STAGES and
            timing_util.EndToEndRuntimeMeasurementEnabled()):
          collector.AddSamples(
              end_to_end_timer.GenerateSamples(), spec.name, spec)
        if timing_util.RuntimeMeasurementsEnabled():
          collector.AddSamples(
              detailed_timer.GenerateSamples(), spec.name, spec)

      except:
        # Resource cleanup (below) can take a long time. Log the error to give
        # immediate feedback, then re-throw.
        logging.exception('Error during benchmark %s', spec.name)
        # If the particular benchmark requests us to always call cleanup, do it
        # here.
        if stages.CLEANUP in FLAGS.run_stage and spec.always_call_cleanup:
          DoCleanupPhase(spec, detailed_timer)
        raise
      finally:
        print 'aborting? ', abort.IsAborted()
        if stages.TEARDOWN in FLAGS.run_stage and not abort.IsAborted():
          spec.Delete()
        events.benchmark_end.send(benchmark_spec=spec)
        # Pickle spec to save final resource state.
        spec.Pickle()
  spec.status = benchmark_status.SUCCEEDED


def RunBenchmarkTask(spec):
  """Task that executes RunBenchmark.

  This is designed to be used with RunParallelProcesses.

  Arguments:
    spec: BenchmarkSpec. The spec to call RunBenchmark with.

  Returns:
    A tuple of BenchmarkSpec, list of samples.
  """
  if _TEARDOWN_EVENT.is_set():
    return spec, []

  # Many providers name resources using run_uris. When running multiple
  # benchmarks in parallel, this causes name collisions on resources.
  # By modifying the run_uri, we avoid the collisions.
  if FLAGS.run_processes > 1:
    spec.config.flags['run_uri'] = FLAGS.run_uri + str(spec.sequence_number)

  collector = SampleCollector()
  try:
    RunBenchmark(spec, collector)
  except BaseException as e:
    msg = 'Benchmark {0}/{1} {2} (UID: {3}) failed.'.format(
        spec.sequence_number, spec.total_benchmarks, spec.name, spec.uid)
    if isinstance(e, KeyboardInterrupt) or FLAGS.stop_after_benchmark_failure:
      logging.error('%s Execution will not continue.', msg)
      _TEARDOWN_EVENT.set()
    else:
      logging.error('%s Execution will continue.', msg)
  finally:
    # We need to return both the spec and samples so that we know
    # the status of the test and can publish any samples that
    # haven't yet been published.
    return spec, collector.samples


def _LogCommandLineFlags():
  result = []
  for name in FLAGS:
    flag = FLAGS[name]
    if flag.present:
      result.append(flag.Serialize())
  logging.info('Flag values:\n%s', '\n'.join(result))


def SetUpPKB():
  """Set globals and environment variables for PKB.

  After SetUpPKB() returns, it should be possible to call PKB
  functions, like benchmark_spec.Prepare() or benchmark_spec.Run().

  SetUpPKB() also modifies the local file system by creating a temp
  directory and storing new SSH keys.
  """
  try:
    _InitializeRunUri()
  except errors.Error as e:
    logging.error(e)
    sys.exit(1)

  # Initialize logging.
  vm_util.GenTempDir()
  log_util.ConfigureLogging(
      stderr_log_level=log_util.LOG_LEVELS[FLAGS.log_level],
      log_path=vm_util.PrependTempDir(LOG_FILE_NAME),
      run_uri=FLAGS.run_uri,
      file_log_level=log_util.LOG_LEVELS[FLAGS.file_log_level])
  logging.info('PerfKitBenchmarker version: %s', version.VERSION)

  # Translate deprecated flags and log all provided flag values.
  disk.WarnAndTranslateDiskFlags()
  _LogCommandLineFlags()

  # Check environment.
  if not FLAGS.ignore_package_requirements:
    requirements.CheckBasicRequirements()

  if FLAGS.os_type == os_types.WINDOWS and not vm_util.RunningOnWindows():
    logging.error('In order to run benchmarks on Windows VMs, you must be '
                  'running on Windows.')
    sys.exit(1)

  for executable in REQUIRED_EXECUTABLES:
    if not vm_util.ExecutableOnPath(executable):
      raise errors.Setup.MissingExecutableError(
          'Could not find required executable "%s"', executable)

  vm_util.SSHKeyGen()

  if FLAGS.static_vm_file:
    with open(FLAGS.static_vm_file) as fp:
      static_virtual_machine.StaticVirtualMachine.ReadStaticVirtualMachineFile(
          fp)

  events.initialization_complete.send(parsed_flags=FLAGS)


def RunBenchmarks():
  """Runs all benchmarks in PerfKitBenchmarker.

  Returns:
    Exit status for the process.
  """
  benchmark_specs = _CreateBenchmarkSpecs()
  collector = SampleCollector()

  try:
    tasks = [(RunBenchmarkTask, (spec,), {})
             for spec in benchmark_specs]
    spec_sample_tuples = background_tasks.RunParallelProcesses(
        tasks, FLAGS.run_processes)
    benchmark_specs, sample_lists = zip(*spec_sample_tuples)
    for sample_list in sample_lists:
      collector.samples.extend(sample_list)

  finally:
    if collector.samples:
      collector.PublishSamples()

    if benchmark_specs:
      logging.info(benchmark_status.CreateSummary(benchmark_specs))

    logging.info('Complete logs can be found at: %s',
                 vm_util.PrependTempDir(LOG_FILE_NAME))


  if stages.TEARDOWN not in FLAGS.run_stage:
    logging.info(
        'To run again with this setup, please use --run_uri=%s', FLAGS.run_uri)

  if FLAGS.archive_bucket:
    archive.ArchiveRun(vm_util.GetTempDir(), FLAGS.archive_bucket,
                       gsutil_path=FLAGS.gsutil_path,
                       prefix=FLAGS.run_uri + '_')
  all_benchmarks_succeeded = all(spec.status == benchmark_status.SUCCEEDED
                                 for spec in benchmark_specs)
  return 0 if all_benchmarks_succeeded else 1


def _GenerateBenchmarkDocumentation():
  """Generates benchmark documentation to show in --help."""
  benchmark_docs = []
  for benchmark_module in (linux_benchmarks.BENCHMARKS +
                           windows_benchmarks.BENCHMARKS):
    benchmark_config = configs.LoadMinimalConfig(
        benchmark_module.BENCHMARK_CONFIG, benchmark_module.BENCHMARK_NAME)
    vm_groups = benchmark_config.get('vm_groups', {})
    total_vm_count = 0
    vm_str = ''
    scratch_disk_str = ''
    for group in vm_groups.itervalues():
      group_vm_count = group.get('vm_count', 1)
      if group_vm_count is None:
        vm_str = 'variable'
      else:
        total_vm_count += group_vm_count
      if group.get('disk_spec'):
        scratch_disk_str = ' with scratch volume(s)'

    name = benchmark_module.BENCHMARK_NAME
    if benchmark_module in windows_benchmarks.BENCHMARKS:
      name += ' (Windows)'
    benchmark_docs.append('%s: %s (%s VMs%s)' %
                          (name,
                           benchmark_config['description'],
                           vm_str or total_vm_count,
                           scratch_disk_str))
  return '\n\t'.join(benchmark_docs)


def Main():
  log_util.ConfigureBasicLogging()
  _InjectBenchmarkInfoIntoDocumentation()
  _ParseFlags()
  if FLAGS.helpmatch:
    _PrintHelp(FLAGS.helpmatch)
    return 0
  CheckVersionFlag()
  SetUpPKB()
  return RunBenchmarks()
