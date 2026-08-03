import io
import os
import sys
import copy
import json
import time
import uuid
import socket
import logging
import traceback
import numpy as np
from threading import local
from collections import OrderedDict
from contextlib import contextmanager

LOG_TIMESTAMPS = "LOG_TIMESTAMPS" in os.environ

def _tmpfunc():
  return 0


def _srcfile():
  return os.path.normcase(_tmpfunc.__code__.co_filename)

def json_handler(obj):
  if isinstance(obj, np.bool_):
    return bool(obj)
  # if isinstance(obj, (datetime.date, datetime.time)):
  #   return obj.isoformat()
  return repr(obj)

def json_robust_dumps(obj):
  return json.dumps(obj, default=json_handler)

class NiceOrderedDict(OrderedDict):
  def __str__(self):
    return json_robust_dumps(self)


class CloudLogger(logging.Logger):
  def __init__(self):
    logging.Logger.__init__(self, "cloudlog")

    self.global_ctx = {}

    self.log_local = local()
    self.log_local.ctx = {}

  def local_ctx(self):
    try:
      return self.log_local.ctx
    except AttributeError:
      self.log_local.ctx = {}
      return self.log_local.ctx

  def get_ctx(self):
    return dict(self.local_ctx(), **self.global_ctx)

  @contextmanager
  def ctx(self, **kwargs):
    old_ctx = self.local_ctx()
    self.log_local.ctx = copy.copy(old_ctx) or {}
    self.log_local.ctx.update(kwargs)
    try:
      yield
    finally:
      self.log_local.ctx = old_ctx

  def bind(self, **kwargs):
    self.local_ctx().update(kwargs)

  def bind_global(self, **kwargs):
    self.global_ctx.update(kwargs)

  def event(self, event, *args, **kwargs):
    evt = NiceOrderedDict()
    evt['event'] = event
    if args:
      evt['args'] = args
    evt.update(kwargs)
    if 'error' in kwargs:
      self.error(evt)
    elif 'debug' in kwargs:
      self.debug(evt)
    else:
      self.info(evt)

  def timestamp(self, event_name):
    if LOG_TIMESTAMPS:
      t = time.monotonic()
      tstp = NiceOrderedDict()
      tstp['timestamp'] = NiceOrderedDict()
      tstp['timestamp']["event"] = event_name
      tstp['timestamp']["time"] = t*1e9
      self.debug(tstp)

  def findCaller(self, stack_info=False, stacklevel=1):
    """
    Find the stack frame of the caller so that we can note the source
    file name, line number and function name.
    """
    f = sys._getframe(3)
    #On some versions of IronPython, currentframe() returns None if
    #IronPython isn't run with -X:Frames.
    if f is not None:
      f = f.f_back
    orig_f = f
    while f and stacklevel > 1:
      f = f.f_back
      stacklevel -= 1
    if not f:
      f = orig_f
    rv = "(unknown file)", 0, "(unknown function)", None
    while hasattr(f, "f_code"):
      co = f.f_code
      filename = os.path.normcase(co.co_filename)

      if filename == _srcfile:
        f = f.f_back
        continue
      sinfo = None
      if stack_info:
        sio = io.StringIO()
        sio.write('Stack (most recent call last):\n')
        traceback.print_stack(f, file=sio)
        sinfo = sio.getvalue()
        if sinfo[-1] == '\n':
          sinfo = sinfo[:-1]
        sio.close()
      rv = (co.co_filename, f.f_lineno, co.co_name, sinfo)
      break
    return rv


cloudlog = log = CloudLogger()
log.setLevel(logging.DEBUG)