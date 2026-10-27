set -l subs doctor setup-ai update off on status help
complete -c fishsense -f
complete -c fishsense -n "not __fish_seen_subcommand_from $subs" -a doctor -d 'Check that everything is in order'
complete -c fishsense -n "not __fish_seen_subcommand_from $subs" -a setup-ai -d 'Install ollama and the model for ctrl+space'
complete -c fishsense -n "not __fish_seen_subcommand_from $subs" -a update -d 'Download the tldr pages again'
complete -c fishsense -n "not __fish_seen_subcommand_from $subs" -a off -d 'Turn fishsense off in every terminal'
complete -c fishsense -n "not __fish_seen_subcommand_from $subs" -a on -d 'Turn fishsense on'
complete -c fishsense -n "not __fish_seen_subcommand_from $subs" -a status -d 'Show how it is working'
complete -c fishsense -n "not __fish_seen_subcommand_from $subs" -a help -d 'Show the help'
