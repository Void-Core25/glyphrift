import importlib  # => imports ?importlib unresolved
import handlers  # => imports handlers resolved

# A function that is only referenced, never called here, produces no edge.
HANDLERS = {"start": handlers.on_start, "stop": handlers.on_stop}


class Job:
    def run(self):
        return 1


def make_job():
    return Job()  # => calls dynamic.Job resolved


def run_job():
    job = make_job()  # => calls dynamic.make_job resolved
    return job.run()  # => calls dynamic.Job.run heuristic


def dispatch(name):
    return HANDLERS[name]()


def by_getattr(name):
    return getattr(handlers, name)()


def plugin(name):
    module = importlib.import_module(name)
    return module.run()  # => calls dynamic.Job.run heuristic


def apply(func):
    return func()  # => calls ?func unresolved


def use_apply():
    return apply(handlers.on_start)  # => calls dynamic.apply resolved
