import subprocess
import os
from error import GitError
SVN = 'svn'

def _setenv(env, name, value):
  env[name] = value.encode()


class SvnCommand(object):
  def __init__(self,
               project,
               cmdv,
               provide_stdin = False,
               capture_stdout = False,
               capture_stderr = False,
               disable_editor = False,
               cwd = None):
    env = os.environ.copy()

    if disable_editor:
      _setenv(env, 'GIT_EDITOR', ':')

    #svn is locale dependent
    _setenv(env, 'LC_ALL', 'C')

    if project:
      if not cwd:
        cwd = project.worktree

    command = [SVN]
    command.extend(cmdv)

    if provide_stdin:
      stdin = subprocess.PIPE
    else:
      stdin = None

    if capture_stdout:
      stdout = subprocess.PIPE
    else:
      stdout = None

    if capture_stderr:
      stderr = subprocess.PIPE
    else:
      stderr = None

    try:
      p = subprocess.Popen(command,
                           cwd = cwd,
                           env = env,
                           stdin = stdin,
                           stdout = stdout,
                           stderr = stderr)
    except Exception, e:
      raise GitError('%s: %s' % (command[1], e))

    self.process = p
    self.stdin = p.stdin

  def Wait(self):
    p = self.process
    (self.stdout, self.stderr) = p.communicate()
    rc = p.returncode
    return rc
