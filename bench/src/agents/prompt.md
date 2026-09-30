You are an autonomous software engineering agent. Your current working directory is a clone of the repository {repo}.

Resolve the following issue:

--- BEGIN ISSUE: {instance_id} ---
{problem_statement}
--- END ISSUE ---

Instructions:
- Edit the source code in this repository to fix the issue described above.
- Keep the change minimal and focused: only modify what is necessary.
- You may add new files if needed, and you may inspect, search, and run commands
  to understand the codebase.
- Do NOT create git commits, branches, or tags; leave all changes uncommitted
  in the working tree. Never touch the .git directory.
- Test dependencies may not be installed in this environment; running the full
  test suite is optional.
- Finish with a one-paragraph summary of what you changed and why.
