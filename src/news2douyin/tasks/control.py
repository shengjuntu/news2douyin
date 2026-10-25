class TaskControlError(Exception):
    """Must propagate through provider/LLM fallback handlers."""


class TaskCancelled(TaskControlError):
    pass


class TaskLeaseLost(TaskControlError):
    pass


class TaskWorkerStopping(TaskControlError):
    pass


class TaskConflict(ValueError):
    pass
