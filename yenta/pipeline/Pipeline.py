import networkx as nx

from collections import defaultdict
from dataclasses import dataclass, field
from pprint import pprint
from typing import List, Dict, Any

from yenta.tasks.Task import TaskDef, ParameterType, ResultSpec, ResultType
from yenta.artifacts.Artifact import Artifact
from yenta.values.Value import Value


class InvalidTaskResultError(Exception):
    pass


class InvalidParameterError(Exception):
    pass


@dataclass
class TaskResult:
    """ Holds the result of a specific task execution """
    values: Dict[str, Value] = field(default_factory=dict)
    artifacts: Dict[str, Artifact] = field(default_factory=dict)


@dataclass
class PipelineResult:
    """ Holds the intermediate results of a step in the pipeline, where the keys of the dicts
        are the names of the tasks that have been executed and the values are TaskResults"""
    task_results: Dict[str, TaskResult] = field(default_factory=dict)

    def values(self, task_name: str, value_name: str):
        return self.task_results[task_name].values[value_name].value

    def artifacts(self, task_name: str, artifact_name: str):
        return self.task_results[task_name].values[artifact_name].value

    def from_spec(self, spec: ResultSpec):
        func = getattr(self, spec.resut_type)
        return func(spec.result_task_name, spec.result_var_name)


class Pipeline:

    def __init__(self, *tasks):

        self._tasks = tasks
        self.task_graph = nx.DiGraph()
        self.execution_order = []

        self.build_task_graph()

    def build_task_graph(self):

        for task in self._tasks:
            self.task_graph.add_node(task.task_def.name, task=task)
            for dependency in (task.task_def.depends_on or []):
                self.task_graph.add_edge(dependency, task.task_def.name)

        self.execution_order = list(nx.algorithms.dag.lexicographical_topological_sort(self.task_graph))

    @staticmethod
    def _wrap_task_output(raw_output, task_name):

        if isinstance(raw_output, dict):
            output: TaskResult = TaskResult(**raw_output)
        elif isinstance(raw_output, TaskResult):
            output = raw_output
        else:
            raise InvalidTaskResultError(f'Task {task_name} returned invalid result of type {type(raw_output)}, '
                                          f'expected either a dict or a TaskResult')

        return output

    def invoke_task(self, task, args: PipelineResult):

        task_def: TaskDef = task.task_def
        if len(task_def.param_specs) == 0:
            output = task()
        elif len(task_def.param_specs) == 1 and task_def.param_specs[0].param_type == ParameterType.PAST_RESULTS:
            output = task(args)
        else:
            args_dict = {}
            for spec in task_def.param_specs:
                if spec.param_type != ParameterType.EXPLICIT:
                    raise InvalidParameterError(f'Only EXPLICIT parameters are allowed for {task_def.name}')
                args_dict[spec.param_name] = args.from_spec(spec.result_spec)
            output = task(**args_dict)

        return self._wrap_task_output(output, task_def.name)

    def run_pipeline(self):

        result = PipelineResult()

        for node in self.execution_order:
            task = self.task_graph.nodes[node]['task']
            args = PipelineResult()
            for dependency in (task.task_def.depends_on or []):
                args.task_results[dependency] = result.task_results[dependency]

            # output = self._wrap_task_output(task(previous_results=args), node)
            output = self.invoke_task(task, args)

            result.task_results[task.task_def.name] = output

        return result
