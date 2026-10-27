function __fishsense_ai --description 'ctrl-space: complete or rewrite the line; `local` forces the local model over fishsense_ai_cmd'
    set -l local 0
    test "$argv[1]" = local; and set local 1
    if not __fishsense_on; or commandline --paging-mode
        test $local = 1; or __fishsense_ai_orig
        return
    end
    set -l buf "$(commandline)"
    __fishsense_fits $buf; or return
    set -l names
    string match -qr '^\s*[^\s;|&<>()$]+$' -- $buf
    and set names (string match -- (string trim -- $buf)'*' (functions -n) (builtin -n) (abbr --list))
    set -q fishsense_ai_model[1]; and set -lx FISHSENSE_AI_MODEL $fishsense_ai_model[1]
    __fishsense_cmd_meta $buf
    function __fishsense_ai_int --on-signal SIGINT
    end
    set -l out
    if set -q fishsense_ai_cmd[1]; and test $local != 1
        set -lx FISHSENSE_CWD "$PWD"
        set -lx FISHSENSE_BUFFER "$buf"
        set -lx FISHSENSE_CMD_CUSTOM $__fsm_custom
        set -lx FISHSENSE_CMD_DESC "$__fsm_desc"
        set -l line (command timeout 30 $fishsense_ai_cmd 2>/dev/null | string collect \
            | string replace -ra '[\x00-\x09\x0b-\x1f\x7f-\x9f]' '' | string trim)
        if test -n "$line" -a "$line" != "$buf"
            if string match -q -- "$buf*" "$line"; and test (string length -- "$line") -gt (string length -- "$buf")
                set out ghost\t(string escape --style=url -- "$line")
            else
                set out replace\t(string escape --style=url -- "$line")
            end
        end
    else
        set out (string join \n -- $names | __fishsense_call ai --cwd (string escape --style=url -- "$PWD") --buffer (string escape --style=url -- "$buf") \
            --fish-data $__fish_user_data_dir --real-name $__fishsense_real_name --pid $fish_pid \
            --cmd-custom $__fsm_custom --cmd-desc (string escape --style=url -- "$__fsm_desc"))
    end
    functions -e __fishsense_ai_int
    commandline -f repaint
    test "$fish_history" = "$__fishsense_shadow"; or __fishsense_resume
    set -l kv (string split -m1 \t -- $out[1])
    test "$kv[1]" != error; and test -w $__fishsense_run; and true >$__fishsense_run/ai.used
    set -l text (string unescape --style=url -- "$kv[2]" | string collect)
    set text (string replace -ra '[\x00-\x09\x0b-\x1f\x7f-\x9f]' '' -- "$text")
    test -n "$text"; or return
    switch $kv[1]
        case ghost replace
            set -l n (string length -- "$buf")
            if test $kv[1] = ghost -a $n -gt 0 -a "$fish_history" = "$__fishsense_shadow"
                and test (string length -- "$text") -gt $n
                and test "$(string sub -l $n -- "$text")" = "$buf"
                and type -q -- (string match -rg '^\s*(\S+)' -- $text)
                __fishsense_inject $text
                commandline -f undo redo
            else
                commandline -r -- $text
            end
        case error
            string repeat -N \n --count=(math (commandline | count) - (commandline --line) + 1)
            set_color --dim
            printf '%s\n' $text
            set_color normal
    end
end

function __fishsense_ai_orig --description 'run the ctrl-space binding __fishsense_bind replaced, for this mode'
    set -l v __fishsense_ai_orig_$fish_bind_mode
    set -q $v; or return 0
    set -l rl (bind --function-names)
    for c in $$v
        if contains -- $c $rl
            commandline -f $c
        else
            eval $c
        end
    end
end
