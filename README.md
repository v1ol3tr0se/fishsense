# fishsense 🐟

fish already guesses what you're going to type, this just helps it guess better

the grey suggestion gets ranked by what folder you're in, what you just ran and how often you run it, not just whatever matched last. and then there's some extra stuff:

- type a command and a space and it shows you how it's used, like `tar ` turns into `tar cf ‹path/to/target.tar› ‹path/to/file1 path/to/file2 ...›`. it learns that from your history, tldr pages, or the command's own usage line
- tab completions for commands that don't come with any (`waifu2x -` + tab gives you `-i`, `-o`, `-s` and so on, and it knows `-o` is required)
- typos get fixed when you hit enter, `gti stauts` → `git status`. anything that could delete stuff asks first
- `ctrl+space` asks a small local model. it gets your files, git, your last commands and how you usually run that command, so it doesn't just make things up. no api key, nothing leaves your computer

### you'll need
- fish 4
- python 3.10+
- [fisher](https://github.com/jorgebucaran/fisher)
- ollama, but only if you want the `ctrl+space` thing

### installing
```shell
fisher install v1ol3tr0se/fishsense
fishsense update
```

the second one downloads the tldr pages

for the model part (optional):

```shell
fishsense setup-ai
```

that turns on ollama and downloads `qwen2.5-coder:3b`, it's around 2 gb. it'll ask for your password once

if something seems off `fishsense doctor` checks everything

### using it
- `→` or `ctrl+e` takes the suggestion, but only up to the first `‹placeholder›`
- `ctrl+space` asks the model. if it continues what you typed you get it as a suggestion, if not it replaces the line (`ctrl+z` undoes that). if it has nothing useful it just doesn't do anything
- when you're plugged in, pressing `space` lets it fill the rest by itself, like the output file for `waifu2x -i pic.png ` or flags you always use
- `Enter` fixes typos in the command or subcommand. `rm`, `dd`, `git push --force`, `sudo …` and stuff like that ask `[Y/n]` first
- `shift+delete` on a suggestion deletes it from your history for good

### settings
- `fishsense_ai_model` which ollama model to use (`qwen2.5-coder:3b` by default), set it with `set -U`
- `fishsense_ai_autospace` the space thing, on by default and only when plugged in. `set -U fishsense_ai_autospace 0` if you only want `ctrl+space`
- `fishsense_ai_cmd` if you'd rather use your own command for `ctrl+space`. it gets the line in `FISHSENSE_BUFFER` and the folder in `FISHSENSE_CWD` and should print one command. space still uses the local model
- `fishsense_disabled` turns the whole thing off
- `fishsense off` / `fishsense on` same but in every open shell at once

oh, if you put a host in `~/.config/target` (just one line) the model uses it for scans (`nmap `, `gobuster `…) instead of guessing

### things it can't do
the suggestion can only continue what you typed, that's how fish draws it

on battery the model runs on the cpu so your gpu can sleep. it's like 1 to 3 seconds per answer, and the first one after an hour of not using it is slower because the model has to load again. your battery will survive, your patience maybe not

while you're using `ctrl+space` it has ollama read your folder ahead of time so it answers faster (not on battery)

private mode (`fish --private`) and commands starting with a space never get saved or suggested. everything else does, so keep that in mind

if it ever suggests something weird i really want to know
