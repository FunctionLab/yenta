from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from inspect import signature, Parameter
from typing import Callable, List, Optional, Any


class ParameterType(int, Enum):

    PIPELINE_RESULTS = 1
    EXPLICIT = 2


class ResultType(str, Enum):

    VALUE = 'values'
    ARTIFACT = 'artifacts'


@dataclass
class ResultSpec:

    result_task_name: str
    resut_type: ResultType
    result_var_name: str


@dataclass
class ParameterSpec:

    param_name: str
    param_type: ParameterType
    result_spec: Optional[ResultSpec] = None


@dataclass
class TaskDef:

    name: str
    depends_on: Optional[List[str]]
    pure: bool
    param_specs: List[ParameterSpec] = field(default_factory=list)


class InvalidTaskDefinitionError(Exception):
    pass


def build_parameter_spec(func):

    sig = signature(func)
    param_names = list(sig.parameters.keys())

    # two options available:
    # 1. a single parameter which will receive the full intermediate pipeline state
    # 2. any number of parameters annotated with a string of the form:
    #   '<task_name>__<values|artifacts>__<value_name|artifact_name>'
    # note the double underbars like in the django query language

    err_format = '<task_name>__<values|artifacts>__<value_name|artifact_name>'

    if len(param_names) == 0:
        spec = []
    elif len(param_names) == 1 and '__' not in param_names[0]:
        spec = [ParameterSpec(param_names[0], ParameterType.PIPELINE_RESULTS)]
    elif len(param_names) > 1:
        spec = []
        for name in param_names:
            param = sig.parameters[name]
            if not isinstance(param.annotation, str):
                raise InvalidTaskDefinitionError(
                    f'Annotation string missing for variable {name}.'
                    f'Function parameters must be annotated using the following format:'
                                                 f'\n{err_format}')
            annot = param.annotation.split('__')
            if len(annot) != 3:
                raise InvalidTaskDefinitionError(
                    f'Invalid function annotation for parameter {name}.'
                    f'Function parameters must be annotated using the following format:'
                    f'\n{err_format}')

            spec.append(ParameterSpec(name, ParameterType.EXPLICIT, ResultSpec(*annot)))

    return spec


def task(_func=None, *, depends_on: str = None, pure: bool = True):

    def decorator_task(func: Callable):

        sig = signature(func)
        param_names = sig.parameters.keys()

        @wraps(func)
        def task_wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        task_wrapper.task_def = TaskDef(
            name=func.__name__,
            depends_on=depends_on,
            pure=pure,
            param_specs=build_parameter_spec(func)
        )

        return task_wrapper

    if _func is None:
        return decorator_task
    else:
        return decorator_task(_func)
