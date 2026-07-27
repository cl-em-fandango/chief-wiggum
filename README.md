```

  ▀▀▀▀▀  ▀▀   ▀▀ ▀▀▀▀▀▀ ▀▀▀▀▀▀▀ ▀▀▀▀▀▀▀      ▀▀     ▀▀ ▀▀▀▀▀▀   ▀▀▀▀▀     ▀▀▀▀▀   ▀▀   ▀▀ ▀▀▀   ▀▀▀
 ▀▀   ▀▀ ▀▀   ▀▀   ▀▀   ▀▀      ▀▀           ▀▀  ▀  ▀▀   ▀▀    ▀▀        ▀▀       ▀▀   ▀▀ ▀▀▀▀ ▀▀▀▀
▀▀▀      ▀▀▀▀▀▀▀   ▀▀   ▀▀▀▀▀   ▀▀▀▀▀        ▀▀ ▀▀▀ ▀▀   ▀▀   ▀▀▀  ▀▀▀▀ ▀▀▀  ▀▀▀▀ ▀▀   ▀▀ ▀▀ ▀▀▀ ▀▀
 ▀▀   ▀▀ ▀▀   ▀▀   ▀▀   ▀▀      ▀▀           ▀▀▀▀ ▀▀▀▀   ▀▀    ▀▀   ▀▀   ▀▀   ▀▀  ▀▀   ▀▀ ▀▀  ▀  ▀▀
  ▀▀▀▀▀  ▀▀   ▀▀ ▀▀▀▀▀▀ ▀▀▀▀▀▀▀ ▀▀            ▀▀   ▀▀  ▀▀▀▀▀▀   ▀▀▀▀▀     ▀▀▀▀▀    ▀▀▀▀▀  ▀▀     ▀▀

###############################-----###########################
###########################----------------------##############
################----------------------------------#############
###############-------------------------------------###########
#############-------------------------------------------#######
############-------------------------------#---------------####
##########-----------------------------####--##-----------#####
#######--------------------------------##-##--#---------#######
###---------------------------#########-##---#--------#########
####-------------------#-----------------------#---############
########---------------#----------------------------###########
#############-----######--##--------------------------#########
#############--##########-###---####-------------------########
############--##########--##-##########--#######--#############
#############--########--##--###-#######-#########-############
##############---#--##--###-############--###-####-############
##############-#####-#-####-############--########-############
##############-###-#--######--#######--#######----#############
#############-----############--------##--##---#-##############
#############-#######################-#-##-###------###########
############--######################################--#########
############--###############--#######################-########
############--################-----##################--########
#############-###############--#######---------------##########
##############--##############################--###############
################--############################--###############
##################---#########################-################
######################----###################--################
############################-----------------##################
```

"Bake em away, toys!"

Chief Wiggum Loop: run nested Ralph loops over a task tree.

Chief Wiggum is a 'kick-it-off-and-walk-away' task runner for local LLMs, not a zero-shot code generator like opencode. It relies on a strict hybrid pipeline: rely on a heavy cloud LLM for the upfront spec work, then let your local LLM grind through the actual coding for free. If you don't aggressively break your project down into small, isolated chunks, your local model will hit context limits, hallucinate, or infinitely loop. Keep your context windows minimal, spec heavily upfront, and Chief Wiggum will handle the rest. 

Your project will make sergeant with this if you do it right. If you are lazy it'll get busted back down to sergeant so fast it'll make your head spin.

This project has some obvious mandatory dependencies:
- Opencode CLI installed and configured to talk to your local llm setup
- opencode-ralph-loop plugin installed to opencode

Thats pretty much it. Not tested on Windows or Mac but expect it would work provided your Python environment is setup correctly. Tested on Debian (full bifter & WSL) & CachyOs Linux.

```
usage: chief_wiggum_loop.py [-h] [--include INCLUDE] [--exclude EXCLUDE] [--ignore-dir IGNORE_DIR] [--max-file-size-kb MAX_FILE_SIZE_KB] [--refresh-seconds REFRESH_SECONDS]
                            [--max-review-passes MAX_REVIEW_PASSES] [--opencode-bin OPENCODE_BIN] [--model MODEL] [--agent AGENT] [--auto] [--allow-dirty-start] [--dry-run] [--sync-mode]
                            [target_dir]

positional arguments:
  target_dir            Directory containing task files to process recursively.

options:
  -h, --help            Show this help message and exit.
  --include INCLUDE     Glob pattern to include, relative to target_dir. Repeatable. Defaults to all text files not excluded.
  --exclude EXCLUDE     Glob pattern to exclude, relative to target_dir. Repeatable.
  --ignore-dir IGNORE_DIR
                        Directory name to skip during traversal. Repeatable.
  --max-file-size-kb MAX_FILE_SIZE_KB
                        Maximum task file size in KB when auto-detecting text tasks.
  --refresh-seconds REFRESH_SECONDS
                        Dashboard refresh interval in seconds.
  --max-review-passes MAX_REVIEW_PASSES
                        Maximum review and commit passes per task file.
  --opencode-bin OPENCODE_BIN
                        Path to the opencode executable.
  --model MODEL         Optional opencode model override.
  --agent AGENT         Optional opencode agent override.
  --auto                Pass --auto to opencode. Use only if your opencode policy allows it.
  --allow-dirty-start   Allow starting when the git working tree already has unrelated changes.
  --dry-run             Scan tasks and write initial state, but do not invoke opencode or git commits.
  --sync-mode           Traverse tasks and refresh implementation state via a Ralph run without review or commits.

Chief says: pick your mode, keep the tree moving, and let the loop finish the job.
```