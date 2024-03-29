#
# Copyright (C) 2008 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import re
import sys

from command import InteractiveCommand
from editor import Editor
from error import HookError, UploadError
from project import RepoHook

UNUSUAL_COMMIT_THRESHOLD = 5

def _ConfirmManyUploads(multiple_branches=False):
  if multiple_branches:
    print "ATTENTION: One or more branches has an unusually high number of commits."
  else:
    print "ATTENTION: You are uploading an unusually high number of commits."
  print "YOU PROBABLY DO NOT MEAN TO DO THIS. (Did you rebase across branches?)"
  answer = raw_input("If you are sure you intend to do this, type 'yes': ").strip()
  return answer == "yes"

def _die(fmt, *args):
  msg = fmt % args
  print >>sys.stderr, 'error: %s' % msg
  sys.exit(1)

def _SplitEmails(values):
  result = []
  for str in values:
    result.extend([s.strip() for s in str.split(',')])
  return result

class Upload(InteractiveCommand):
  common = True
  helpSummary = "Upload changes for code review"
  helpUsage="""
%prog [--re --cc] [<project>]...
"""
  helpDescription = """
The '%prog' command is used to send changes to the Gerrit Code
Review system.  It searches for topic branches in local projects
that have not yet been published for review.  If multiple topic
branches are found, '%prog' opens an editor to allow the user to
select which branches to upload.

'%prog' searches for uploadable changes in all projects listed at
the command line.  Projects can be specified either by name, or by
a relative or absolute path to the project's local directory. If no
projects are specified, '%prog' will search for uploadable changes
in all projects listed in the manifest.

If the --reviewers or --cc options are passed, those emails are
added to the respective list of users, and emails are sent to any
new users.  Users passed as --reviewers must already be registered
with the code review system, or the upload will fail.

Configuration
-------------

review.URL.autoupload:

To disable the "Upload ... (y/N)?" prompt, you can set a per-project
or global Git configuration option.  If review.URL.autoupload is set
to "true" then repo will assume you always answer "y" at the prompt,
and will not prompt you further.  If it is set to "false" then repo
will assume you always answer "n", and will abort.

review.URL.autocopy:

To automatically copy a user or mailing list to all uploaded reviews,
you can set a per-project or global Git option to do so. Specifically,
review.URL.autocopy can be set to a comma separated list of reviewers
who you always want copied on all uploads with a non-empty --re
argument.

review.URL.username:

Override the username used to connect to Gerrit Code Review.
By default the local part of the email address is used.

The URL must match the review URL listed in the manifest XML file,
or in the .git/config within the project.  For example:

  [remote "origin"]
    url = git://git.example.com/project.git
    review = http://review.example.com/

  [review "http://review.example.com/"]
    autoupload = true
    autocopy = johndoe@company.com,my-team-alias@company.com

review.URL.uploadtopic:

To add a topic branch whenever uploading a commit, you can set a
per-project or global Git option to do so. If review.URL.uploadtopic
is set to "true" then repo will assume you always want the equivalent
of the -t option to the repo command. If unset or set to "false" then
repo will make use of only the command line option.

References
----------

Gerrit Code Review:  http://code.google.com/p/gerrit/

"""

  def _Options(self, p):
    p.add_option('-t',
                 dest='auto_topic', action='store_true',
                 help='Send local branch name to Gerrit Code Review')
    p.add_option('--re', '--reviewers',
                 type='string',  action='append', dest='reviewers',
                 help='Request reviews from these people.')
    p.add_option('--cc',
                 type='string',  action='append', dest='cc',
                 help='Also send email to these email addresses.')
    p.add_option('--br',
                 type='string',  action='store', dest='branch',
                 help='Branch to upload.')
    p.add_option('--cbr', '--current-branch',
                 dest='current_branch', action='store_true',
                 help='Upload current git branch.')

    # Options relating to upload hook.  Note that verify and no-verify are NOT
    # opposites of each other, which is why they store to different locations.
    # We are using them to match 'git commit' syntax.
    #
    # Combinations:
    # - no-verify=False, verify=False (DEFAULT):
    #   If stdout is a tty, can prompt about running upload hooks if needed.
    #   If user denies running hooks, the upload is cancelled.  If stdout is
    #   not a tty and we would need to prompt about upload hooks, upload is
    #   cancelled.
    # - no-verify=False, verify=True:
    #   Always run upload hooks with no prompt.
    # - no-verify=True, verify=False:
    #   Never run upload hooks, but upload anyway (AKA bypass hooks).
    # - no-verify=True, verify=True:
    #   Invalid
    p.add_option('--no-verify',
                 dest='bypass_hooks', action='store_true',
                 help='Do not run the upload hook.')
    p.add_option('--verify',
                 dest='allow_all_hooks', action='store_true',
                 help='Run the upload hook without prompting.')

  def _SingleBranch(self, opt, branch, people):
    project = branch.project
    name = branch.name
    remote = project.GetBranch(name).remote

    key = 'review.%s.autoupload' % remote.review
    answer = project.config.GetBoolean(key)

    if answer is False:
      _die("upload blocked by %s = false" % key)

    if answer is None:
      date = branch.date
      list = branch.commits

      print 'Upload project %s/ to remote branch %s:' % (project.relpath, project.revisionExpr)
      print '  branch %s (%2d commit%s, %s):' % (
                    name,
                    len(list),
                    len(list) != 1 and 's' or '',
                    date)
      for commit in list:
        print '         %s' % commit

      sys.stdout.write('to %s (y/N)? ' % remote.review)
      answer = sys.stdin.readline().strip()
      answer = answer in ('y', 'Y', 'yes', '1', 'true', 't')

    if answer:
      if len(branch.commits) > UNUSUAL_COMMIT_THRESHOLD:
        answer = _ConfirmManyUploads()

    if answer:
      self._UploadAndReport(opt, [branch], people)
    else:
      _die("upload aborted by user")

  def _MultipleBranches(self, opt, pending, people):
    projects = {}
    branches = {}

    script = []
    script.append('# Uncomment the branches to upload:')
    for project, avail in pending:
      script.append('#')
      script.append('# project %s/:' % project.relpath)

      b = {}
      for branch in avail:
        name = branch.name
        date = branch.date
        list = branch.commits

        if b:
          script.append('#')
        script.append('#  branch %s (%2d commit%s, %s) to remote branch %s:' % (
                      name,
                      len(list),
                      len(list) != 1 and 's' or '',
                      date,
                      project.revisionExpr))
        for commit in list:
          script.append('#         %s' % commit)
        b[name] = branch

      projects[project.relpath] = project
      branches[project.name] = b
    script.append('')

    script = [ x.encode('utf-8')
             if issubclass(type(x), unicode)
             else x
             for x in script ]

    script = Editor.EditString("\n".join(script)).split("\n")

    project_re = re.compile(r'^#?\s*project\s*([^\s]+)/:$')
    branch_re = re.compile(r'^\s*branch\s*([^\s(]+)\s*\(.*')

    project = None
    todo = []

    for line in script:
      m = project_re.match(line)
      if m:
        name = m.group(1)
        project = projects.get(name)
        if not project:
          _die('project %s not available for upload', name)
        continue

      m = branch_re.match(line)
      if m:
        name = m.group(1)
        if not project:
          _die('project for branch %s not in script', name)
        branch = branches[project.name].get(name)
        if not branch:
          _die('branch %s not in %s', name, project.relpath)
        todo.append(branch)
    if not todo:
      _die("nothing uncommented for upload")

    many_commits = False
    for branch in todo:
      if len(branch.commits) > UNUSUAL_COMMIT_THRESHOLD:
        many_commits = True
        break
    if many_commits:
      if not _ConfirmManyUploads(multiple_branches=True):
        _die("upload aborted by user")

    self._UploadAndReport(opt, todo, people)

  def _AppendAutoCcList(self, branch, people):
    """
    Appends the list of users in the CC list in the git project's config if a
    non-empty reviewer list was found.
    """

    name = branch.name
    project = branch.project
    key = 'review.%s.autocopy' % project.GetBranch(name).remote.review
    raw_list = project.config.GetString(key)
    if not raw_list is None and len(people[0]) > 0:
      people[1].extend([entry.strip() for entry in raw_list.split(',')])

  def _FindGerritChange(self, branch):
    last_pub = branch.project.WasPublished(branch.name)
    if last_pub is None:
      return ""

    refs = branch.GetPublishedRefs()
    try:
      # refs/changes/XYZ/N --> XYZ
      return refs.get(last_pub).split('/')[-2]
    except:
      return ""

  def _UploadAndReport(self, opt, todo, original_people):
    have_errors = False
    for branch in todo:
      try:
        people = copy.deepcopy(original_people)
        self._AppendAutoCcList(branch, people)

        # Check if there are local changes that may have been forgotten
        if branch.project.HasChanges():
            key = 'review.%s.autoupload' % branch.project.remote.review
            answer = branch.project.config.GetBoolean(key)

            # if they want to auto upload, let's not ask because it could be automated
            if answer is None:
                sys.stdout.write('Uncommitted changes in ' + branch.project.name + ' (did you forget to amend?). Continue uploading? (y/N) ')
                a = sys.stdin.readline().strip().lower()
                if a not in ('y', 'yes', 't', 'true', 'on'):
                    print >>sys.stderr, "skipping upload"
                    branch.uploaded = False
                    branch.error = 'User aborted'
                    continue

        # Check if topic branches should be sent to the server during upload
        if opt.auto_topic is not True:
           key = 'review.%s.uploadtopic' % branch.project.remote.review
           opt.auto_topic = branch.project.config.GetBoolean(key)

        branch.UploadForReview(people, auto_topic=opt.auto_topic)
        branch.uploaded = True
      except UploadError, e:
        branch.error = e
        branch.uploaded = False
        have_errors = True

    print >>sys.stderr, ''
    print >>sys.stderr, '----------------------------------------------------------------------'

    if have_errors:
      for branch in todo:
        if not branch.uploaded:
          if len(str(branch.error)) <= 30:
            fmt = ' (%s)'
          else:
            fmt = '\n       (%s)'
          print >>sys.stderr, ('[FAILED] %-15s %-15s' + fmt) % (
                 branch.project.relpath + '/', \
                 branch.name, \
                 str(branch.error))
      print >>sys.stderr, ''

    for branch in todo:
        if branch.uploaded:
          print >>sys.stderr, '[OK    ] %-15s %s' % (
                 branch.project.relpath + '/',
                 branch.name)

    if have_errors:
      sys.exit(1)

  def Execute(self, opt, args):
    project_list = self.GetProjects(args)
    pending = []
    reviewers = []
    cc = []
    branch = None

    if opt.branch:
      branch = opt.branch

    for project in project_list:
      if opt.current_branch:
        cbr = project.CurrentBranch
        avail = [project.GetUploadableBranch(cbr)] if cbr else None
      else:
        avail = project.GetUploadableBranches(branch)
      if avail:
        pending.append((project, avail))

    if pending and (not opt.bypass_hooks):
      hook = RepoHook('pre-upload', self.manifest.repo_hooks_project,
                      self.manifest.topdir, abort_if_user_denies=True)
      pending_proj_names = [project.name for (project, avail) in pending]
      try:
        hook.Run(opt.allow_all_hooks, project_list=pending_proj_names)
      except HookError, e:
        print >>sys.stderr, "ERROR: %s" % str(e)
        return

    if opt.reviewers:
      reviewers = _SplitEmails(opt.reviewers)
    if opt.cc:
      cc = _SplitEmails(opt.cc)
    people = (reviewers,cc)

    if not pending:
      print >>sys.stdout, "no branches ready for upload"
    elif len(pending) == 1 and len(pending[0][1]) == 1:
      self._SingleBranch(opt, pending[0][1][0], people)
    else:
      self._MultipleBranches(opt, pending, people)
