function __fishsense_correct --description 'Enter: fix a mistyped command or subcommand before the line runs'
    set -l line "$(commandline)"
    set -l r (__fishsense_correct_once $line); or return
    set -l r2 (__fishsense_correct_once $r[2])
    if set -q r2[2]
        test "$r[1]" = ask; and set r2[1] ask
        set r $r2
    end
    if test "$r[1]" = ask
        echo
        set -l q "did you mean: $r[2]? [Y/n] "
        if test (string length --visible -- $q) -ge $COLUMNS
            printf 'did you mean: %s?\n' $r[2]
            set q '[Y/n] '
        end
        read -l -n 1 -P $q ans; or return
        contains -- "$ans" '' y Y; or return
    end
    commandline -r -- $r[2]
end

function __fishsense_correct_once -a line --description 'prints fix|ask and the corrected line, or fails'
    string match -q ' *' -- "$line"; and return 1
    string match -qr '\n' -- "$line"; and return 1
    printf '%s\n' "$line" | read -lat toks
    string match -qr '^\w[\w.+-]*$' -- "$toks[1]"; or return 1
    if contains -- $toks[1] sudo doas run0 pkexec; and string match -qr '^\w[\w.+-]*$' -- "$toks[2]"
        set -e toks[1]
    end
    set -l cmd $toks[1]
    set -l arg $toks[2]
    string match -qr '^[|;&<>]|^\d*[<>]' -- "$arg"; and set arg ''

    set -l input
    if not type -q -- $cmd; and not abbr -q -- $cmd
        set input '#cmds' (builtin -n) '#funcs' (functions -n)
    else
        string match -qr '^[a-z][\w-]+$' -- "$arg"; or return 1
        test -e "$arg"; and return 1
        abbr -q -- $arg; and return 1
        set -l subs (__fishsense_subcommands $cmd)
        set -q subs[1]; or return 1
        string match -q -- "$arg*" $subs; and return 1
        set subs (__fishsense_subcommands $cmd --fresh)
        string match -q -- "$arg*" $subs; and return 1
        set input "#sub $cmd" $subs
    end

    set -l out (printf '%s\n' $input | __fishsense_call correct --cwd (string escape --style=url -- "$PWD") \
        --line (string escape --style=url -- "$line") --fish-data $__fish_user_data_dir --real-name $__fishsense_real_name)
    set -l kv (string split -m1 \t -- $out[1])
    set -l new (string unescape --style=url -- "$kv[2]" | string collect)
    set new (string replace -ra '[\x00-\x1f\x7f-\x9f]' '' -- "$new")
    test -n "$new" -a "$new" != "$line"; and contains -- "$kv[1]" fix ask; or return 1
    printf '%s\n' $kv[1] $new
end

function __fishsense_subcommands -a cmd fresh --description 'subcommand names fish completes for cmd, cached for a day; --fresh: here and now'
    set -l var __fishsense_sub_(string escape --style=var -- $cmd)
    if test -n "$fresh"; or not set -q $var
        set -l file $__fishsense_cache/subcommands/(string escape --style=url -- $cmd)
        if test -z "$fresh"; and test -f $file; and test (path mtime --relative -- $file) -lt 86400
            set -g $var (string match '*' <$file)
        else
            set -l re 'seen_subcommand_from|use_subcommand|needs_command|using_command|no_subcommand'
            set -l hit
            for f in (path filter -- $fish_complete_path/$cmd.fish)
                string match -qr $re <$f; and set hit 1
            end
            status get-file completions/$cmd.fish 2>/dev/null | string match -qr $re; and set hit 1
            set -g $var
            set -q hit[1]; and set -g $var (complete -C "$cmd " | string replace -r '\t(?!(?i:alias)).*' '' | string match -rv '^-' | path filter -v)
            set -l dir (path dirname $file)
            command mkdir -p $dir 2>/dev/null
            test -w $dir; and begin
                not test -e $file; or test -w $file
            end
            and string join \n -- $$var >$file
        end
    end
    string join \n -- $$var
end
