# tony-stock
This repo helps my stock investing process. It heavily depends on AI agents, Codex and Claude.

## Set up
- Run `bash init.sh` to clone (or pull) the sub-repos below under the root directory of the tony-stock repo. They are gitignored (not submodules).
  ```
  git@github.com:hczhu/smart-stock.git      -> smart-stock/
  git@github.com:hczhu/Logseq-files.git     -> Logseq-files/
  https://github.com/hczhu/code_recipes     -> code_recipes/
  git@github.com:hczhu/stock-research.git   -> stock-research/
  git@github.com:hczhu/learning-notes.git   -> learning-notes/
  ```
- Install gws cli skills for the user (not only for the project) from https://github.com/googleworkspace/cli/tree/main/skills
- Ask me to auth into gws

## Stock trading record spreadsheet
- The spreadsheet id is 1oxtcfl2V4ff3eUMW4954IChpx9eFAoB83QMrZERPSgA
- Each year has a sheet
- Rows are sorted in reverse chronological order
- A deposit record with a negative amount is actually a withdraw
- When adding a row, remember to copy Name and Diversity from other recent rows with the same ticker.
- When adding a row, keep the value type of "Date" column as date type instead of string.
