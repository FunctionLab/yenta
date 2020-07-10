import io
import json
import networkx as nx
import logging
import shutil
import tempfile

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Union, Any, Callable
from enum import Enum
from more_itertools import split_after
from colorama import Fore, Style

from yenta.config import settings
from yenta.tasks.Task import TaskDef, ParameterType, ResultSpec
from yenta.artifacts.Artifact import Artifact
from yenta.values.Value import Value


logger = logging.getLogger(__name__)


class InvalidTaskResultError(Exception):
    pass


class InvalidParameterError(Exception):
    pass


class TaskStatus(str, Enum):

    SUCCESS = 'success'
    FAILURE = 'failure'


@dataclass
class TaskResult:
    """ Holds the result of a specific task execution """

    values: Dict[str, Union[Value, List, str, int, float, bool]] = field(default_factory=dict)
    """ A dictionary whose keys are value names and whose values are... values."""

    artifacts: Dict[str, Artifact] = field(default_factory=dict)
    """ A dictionary whose keys are artifact names and whose values are Artifacts."""

    status: TaskStatus = None
    """ Whether the task succeeded or failed."""

    error: str = None
    """ Error message associated with task failure."""

    @staticmethod
    def _wrap_as_value(v: Union[Value, dict, str, int, float, bool, list]) -> Value:
        """
        Wraps a value resulting from a task execution in a Value.
        :param v: the value to be wrapped
        :return: Value(v)
        """
        if isinstance(v, Value):
            return v
        elif isinstance(v, dict):
            return Value(**v)
        elif isinstance(v, str) or isinstance(v, int) or isinstance(v, float) or isinstance(v, bool) or \
                isinstance(v, list) or v is None:
            return Value(v)
        else:
            raise ValueError(f'Can not wrap {v} in a Value')

    def __post_init__(self):
        """
        Deserialize nested values and artifacts into Value and Artifact objects
        :return:
        """

        values = {k: self._wrap_as_value(v) for k, v in self.values.items() if not isinstance(v, Value)}
        artifacts = {k: Artifact(**v) for k, v in self.artifacts.items() if not isinstance(v, Artifact)}

        self.values.update(values)
        self.artifacts.update(artifacts)


@dataclass
class PipelineResult:
    """ Holds the intermediate results of a step in the pipeline, where the keys of the dicts
        are the names of the tasks that have been executed and the values are TaskResults"""

    task_results: Dict[str, TaskResult] = field(default_factory=dict)
    """ A dictionary whose keys are task names and whose values are the results of that task execution."""

    task_inputs: Dict[str, 'PipelineResult'] = field(default_factory=dict)
    """ A dictionary whose keys are task names and whose values are the inputs used in executing that task."""

    def __post_init__(self):
        """
        Deserialize subcomponents of the PipelineResult into task results and task inputs
        :return: None
        """

        task_results = {k: TaskResult(**v) for k, v in self.task_results.items() if not isinstance(v, TaskResult)}
        task_inputs = {k: PipelineResult(**v) for k, v in self.task_inputs.items() if not isinstance(v, PipelineResult)}

        self.task_results.update(task_results)
        self.task_inputs.update(task_inputs)

    def values(self, task_name: str, value_name: str):
        """ Return the value named `value_name` that was produced by task `task_name`.

        :param str task_name: The name of the task
        :param str value_name: The name of the value
        :return: the unwrapped value produced by the task
        :rtype: Union[list, int, bool, float, str]
        """
        return self.task_results[task_name].values[value_name].value

    def artifacts(self, task_name: str, artifact_name: str):
        """ Return the artifact names `artifact_name` that was produced by the task `task_name`.

        :param str task_name: The name of the task
        :param str artifact_name: The name of the artifact
        :return: The artifact produced by the task
        :rtype: Artifact
        """
        return self.task_results[task_name].artifacts[artifact_name]

    def from_spec(self, spec: ResultSpec):
        """ Return either the value or the artifact of a given task, as computed by
            a ResultSpec. Delegates the actual work to the `value` and `artifacts` functions.

        :param ResultSpec spec: The result spec
        :return: either the value or the artifact computed from the spec
        """
        func = getattr(self, spec.result_type)
        return func(spec.result_task_name, spec.result_var_name)


class Pipeline:

    def __init__(self, *tasks):

        self._tasks = tasks
        self.task_graph = nx.DiGraph()
        self.execution_order = []

        self.build_task_graph()

        self._tasks_executed = set()
        self._tasks_reused = set()

    def build_task_graph(self) -> None:
        """ Construct the task graph for the pipeline

        :return: None
        """
        logger.debug('Building task graph')
        for task in self._tasks:
            self.task_graph.add_node(task.task_def.name, task=task)
            for dependency in (task.task_def.depends_on or []):
                self.task_graph.add_edge(dependency, task.task_def.name)

        logger.debug('Computing execution order')
        try:
            self.execution_order = list(nx.algorithms.dag.lexicographical_topological_sort(self.task_graph))
        except nx.NetworkXUnfeasible as ex:
            print(Fore.RED + 'Unable to build execution graph because pipeline contains cyclic dependencies.')
            raise ex

    @staticmethod
    def _wrap_task_output(raw_output: Union[dict, TaskResult], task_name: str) -> TaskResult:
        """ Wrap the raw output of a task in a TaskResult.

        :param Union[dict, TaskResult] raw_output: The raw output of a task.
        :param task_name: The name of the task.
        :return: A TaskResult containing the output
        :rtype: TaskResult
        """

        if isinstance(raw_output, dict):
            output: TaskResult = TaskResult(**raw_output)
        elif isinstance(raw_output, TaskResult):
            output = raw_output
        else:
            raise InvalidTaskResultError(f'Task {task_name} returned invalid result of type {type(raw_output)}, '
                                         f'expected either a dict or a TaskResult')

        return output

    @staticmethod
    def build_args_dict(task, args: PipelineResult) -> Dict[str, Any]:
        """ Build the args dictionary for executing a task.

        :param task: The task itself, which has a `task_def` attached to it.
        :param PipelineResult args: The results of the pipeline up to this point
        :return: A dictionary whose keys correspond to the arguments expected by
                the task to be executed, and whose values are the values to be
                passed in.
        :rtype: Dict[str, Any]
        """

        logger.debug('Building args dictionary')
        task_def: TaskDef = task.task_def
        args_dict = {}

        for spec in task_def.param_specs:
            if spec.param_type == ParameterType.PIPELINE_RESULTS:
                args_dict[spec.param_name] = args
            elif spec.param_type == ParameterType.EXPLICIT:
                if spec.selector:
                    args_dict[spec.param_name] = spec.selector(args)
                elif spec.result_spec:
                    args_dict[spec.param_name] = args.from_spec(spec.result_spec)

        return args_dict

    def invoke_task(self, task, **kwargs) -> TaskResult:
        """ Call the function that represents the task with the supplied kwargs.

        :param Callable task: The task function.
        :param dict kwargs: The arguments obtained from `build_args`.
        :return: The task result
        :rtype: TaskResult
        """

        output = task(**kwargs)
        return self._wrap_task_output(output, task.task_def.name)

    @staticmethod
    def merge_pipeline_results(res1: PipelineResult, res2: PipelineResult) -> PipelineResult:
        """ Combine two different pipeline results. If they share keys,
            the results of the second pipeline will overwrite those of
            the first.

        :param PipelineResult res1: The first result.
        :param PipelineResult res2: The second result.
        :return: The merged result.
        :rtype: PipelineResult
        """

        return PipelineResult(task_results={**res1.task_results, **res2.task_results},
                              task_inputs={**res1.task_inputs, **res2.task_inputs})

    @staticmethod
    def cache_pipeline_result(cache_file: io.BufferedRandom, result: PipelineResult):
        """ Write the pipeline results to a file.

        :param Path cache_file: The file to which the results should be written.
        :param PipelineResult result: The results.
        :return: None
        """

        try:
            result = json.dumps(asdict(result), indent=4)
            cache_file.truncate(0)
            cache_file.flush()
            cache_file.write(result.encode('utf-8'))
            cache_file.flush()
        except json.JSONDecodeError as ex:
            logger.error(f'Failed to encode pipeline result: {ex}')
            raise InvalidTaskResultError(str(ex))

    @staticmethod
    def load_pipeline() -> PipelineResult:
        """ Load a pipeline from file.

        :return: The pipeline.
        :rtype: PipelineResult
        """
        logger.debug(f'Loading pipeline from {settings.YENTA_JSON_STORE_PATH}')
        if settings.YENTA_JSON_STORE_PATH.exists():
            with open(settings.YENTA_JSON_STORE_PATH, 'r') as f:
                pipeline = PipelineResult(**json.load(f))
        else:
            pipeline = PipelineResult()
        return pipeline

    @staticmethod
    def reuse_inputs(task_name: str, previous_result: PipelineResult, args: PipelineResult) -> bool:
        """ Determine whether inputs from the previous instance of this task should be reused
            or whether the task should be executed again.

        :param str task_name: The name of the task.
        :param PipelineResult previous_result: The previous pipeline result.
        :param PipelineResult args: The arguments with which this task is being called.
        :return: True or False
        :rtype: bool
        """
        previous_inputs = previous_result.task_inputs.get(task_name, None)
        if previous_inputs and previous_result.task_results.get(task_name).status == TaskStatus.SUCCESS:
            return previous_inputs == args

        return False

    def run_pipeline(self, up_to: str = None, force_rerun: List[str] = None) -> PipelineResult:
        """ Execute the tasks in the pipeline.

        :param str up_to: If supplied, execute the pipeline only up to this task.
        :param List[str] force_rerun: Optionally force the listed tasks to be executed.
        :return: The final pipeline state.
        :rtype: PipelineResult
        """

        previous_result: PipelineResult = self.load_pipeline()
        result = PipelineResult()
        self._tasks_reused.clear()
        self._tasks_executed.clear()

        with tempfile.NamedTemporaryFile(delete=False) as cache:

            for task_name in list(split_after(self.execution_order, lambda x: x == up_to))[0]:
                logger.debug(f'Starting executions of {task_name}')
                task = self.task_graph.nodes[task_name]['task']
                args = PipelineResult()
                dependencies_succeeded = True
                for dependency in (task.task_def.depends_on or []):
                    args.task_results[dependency] = result.task_results[dependency]
                    if result.task_results[dependency].status == TaskStatus.FAILURE:
                        dependencies_succeeded = False

                if dependencies_succeeded:
                    if task.task_def.pure and task_name not in (force_rerun or []) and \
                            self.reuse_inputs(task_name, previous_result, args):
                        logger.debug(f'Reusing previous results of {task_name}')
                        self._tasks_reused.add(task_name)
                        output = previous_result.task_results[task_name]
                        marker = Fore.YELLOW + u'\u2014' + Fore.WHITE
                    else:
                        args_dict = self.build_args_dict(task, args)
                        try:
                            logger.debug(f'Calling function to execute {task_name}')
                            output = self.invoke_task(task, **args_dict)
                            output.status = TaskStatus.SUCCESS
                            marker = Fore.GREEN + u'\u2714' + Fore.WHITE
                            self._tasks_executed.add(task_name)
                        except Exception as ex:
                            logger.error(f'Caught exception executing {task_name}: {ex}')
                            output = TaskResult(status=TaskStatus.FAILURE, error=str(ex))
                            marker = Fore.RED + u'\u2718' + Fore.WHITE

                    print(Fore.WHITE + Style.BRIGHT + f'[{marker}] {task_name}')

                    result.task_results[task_name] = output
                    result.task_inputs[task_name] = args

                    self.cache_pipeline_result(cache, self.merge_pipeline_results(previous_result, result))

            cache.seek(0)
            full_results = cache.read().decode('utf-8').strip('\x00')
            with open(settings.YENTA_JSON_STORE_PATH, 'w') as f:
                f.write(full_results)

        return result
