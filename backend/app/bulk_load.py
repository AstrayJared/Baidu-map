"""Loading the city-wide data files without shutting the other threads out."""
from contextlib import contextmanager
import gc
import json
import threading


def _hand_over(value):
    # Being Python, this call is where the interpreter gives the GIL to a thread
    # that has been waiting for it; the C parser on its own never does.
    return value


def loads(text, *, checkpoint=None):
    """``json.loads`` for data files of tens or hundreds of megabytes.

    The C parser holds the GIL for a whole document: the 283 MB graph cache is
    half a minute in which no other thread runs, the event loop included, so
    nothing is answered (status polls neither). Called back once per object, it
    lets them in every few milliseconds, for a fraction more parse time.
    """
    if checkpoint is None:
        return json.loads(text, object_hook=_hand_over)
    objects = 0
    def checked(value):
        nonlocal objects
        objects += 1
        if objects % 1024 == 0:
            checkpoint()
        return value
    checkpoint()
    return json.loads(text, object_hook=checked)


_lock = threading.Lock()
_open = 0
_threshold = None


@contextmanager
def long_lived():
    """Builds something that stays for the life of the process.

    A full cyclic collection walks every tracked object with the GIL held,
    seconds in which nothing else is answered (status polls included), and a
    parse of millions of objects sets one off again and again. So: one now,
    while the heap is still small; none while anything is being built, where it
    would only find the parse in use; and what was built frozen afterwards, out
    of every later one. The young generations are collected as usual
    throughout, and reference counting still frees frozen objects.

    The thresholds are the process's: builds on several threads share one
    suspension, lifted when the last of them is done.
    """
    global _open, _threshold
    with _lock:
        if _open == 0:
            gc.collect()
            _threshold = gc.get_threshold()
            gc.set_threshold(_threshold[0], _threshold[1], 2**31 - 1)
        _open += 1
    built = False
    try:
        yield
        built = True
    finally:
        with _lock:
            _open -= 1
            if _open == 0:
                if built:
                    gc.freeze()
                gc.set_threshold(*_threshold)
