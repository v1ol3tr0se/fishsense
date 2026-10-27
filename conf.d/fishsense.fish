status is-interactive; or return
set -q fish_private_mode; and return
set -q fish_history; and test -z "$fish_history"; and return
command -q python3; or return
test -e $__fish_user_data_dir; and not test -O $__fish_user_data_dir; and return
set -g __fishsense_conf (status filename)
set -q fishsense_disabled; and return

if set -q FISHSENSE_HELPER[1]
    set -g __fishsense_py $FISHSENSE_HELPER
else
    set -g __fishsense_py (status dirname)/fishsense/fishsense.py
end
set -l cache ~/.cache
test -n "$XDG_CACHE_HOME"; and set cache $XDG_CACHE_HOME
set -l data ~/.local/share
test -n "$XDG_DATA_HOME"; and set data $XDG_DATA_HOME
set -g __fishsense_cache $cache/fishsense
if test -n "$XDG_RUNTIME_DIR"
    set -g __fishsense_run $XDG_RUNTIME_DIR/fishsense
else
    set -g __fishsense_run $__fishsense_cache/run
end
test -d $__fishsense_run; or command mkdir -p -m 700 $__fishsense_run
test -d $data/fishsense; or command mkdir -p -m 700 $data/fishsense
test -d $__fishsense_cache; or command mkdir -p $__fishsense_cache
contains -- $__fishsense_cache/completions $fish_complete_path
or set -g fish_complete_path $fish_complete_path $__fishsense_cache/completions

set -g __fishsense_real_name fish
set -q fish_history[1]; and set __fishsense_real_name $fish_history[1]
set -g __fishsense_shadow fishsense_$fish_pid
set -g __fishsense_shadow_file $__fish_user_data_dir/$__fishsense_shadow"_history"
set -g __fishsense_result $__fishsense_run/$fish_pid.result
set -g __fishsense_req 0
set -g __fishsense_rank_seq 0
set -g __fishsense_last ''
set -g __fishsense_checked
set -g __fishsense_tries 0

for f in $__fish_user_data_dir/fishsense_*_history $__fish_user_data_dir/.fishsense_*_history.*.tmp
    test -d /proc/(string match -rg 'fishsense_(\d+)_history' -- $f); or command rm -f -- $f
end
for f in $data/fishsense/.log.tsv.*.tmp
    test -d /proc/(string match -rg '\.(\d+)\.tmp$' -- $f); or command rm -f -- $f
end
test -e $__fishsense_result; and command rm -f -- $__fishsense_result

function __fishsense_on
    not set -q fishsense_disabled; and not set -q fish_private_mode; and not status is-interactive-read
    and not string match -q '' -- $fish_history
end

function __fishsense_fits --description 'Linux caps one argument at 128 KiB; past that fish prints an exec error'
    test (string length -- "$argv") -lt 100000
end

function __fishsense_bg --description 'run the helper detached; it answers through SIGUSR1'
    __fishsense_fits $argv; or return
    set -lx FISHSENSE_ARGS (string join \x1f -- $argv[2..])
    set -lx PYTHONPYCACHEPREFIX $__fishsense_cache/pycache
    set -l err /dev/null
    set -q FISHSENSE_DEBUG; and set err $__fishsense_cache/debug.log
    command sh -c 'setsid python3 "$@" </dev/null >/dev/null 2>>"$0" &' $err $__fishsense_py $argv[1]
end

function __fishsense_call --description 'run the helper and wait for its answer'
    __fishsense_fits $argv; or return
    set -lx FISHSENSE_ARGS (string join \x1f -- $argv[2..])
    set -lx PYTHONPYCACHEPREFIX $__fishsense_cache/pycache
    if set -q FISHSENSE_DEBUG
        command python3 $__fishsense_py $argv[1]
    else
        command python3 $__fishsense_py $argv[1] 2>/dev/null
    end
end

function __fishsense_rank
    set -g __fishsense_rank_seq (math $__fishsense_rank_seq + 1)
    test -d $__fishsense_run; or command mkdir -p -m 700 $__fishsense_run
    echo $__fishsense_rank_seq >$__fishsense_run/$fish_pid.rank
    __fishsense_bg rank --pid $fish_pid --seq $__fishsense_rank_seq --fish-data $__fish_user_data_dir --real-name $__fishsense_real_name \
        --cwd (string escape --style=url -- "$PWD") --last (string escape --style=url -- "$__fishsense_last") --notify $argv
    __fishsense_ai_warm
end

function __fishsense_ai_warm --description 'ctrl-space used this past hour: ollama reads the new context ahead of the key'
    set -l age (path mtime --relative -- $__fishsense_run/ai.used 2>/dev/null); and test $age -lt 3600; or return 0
    set -q fishsense_ai_model[1]; and set -lx FISHSENSE_AI_MODEL $fishsense_ai_model[1]
    __fishsense_bg ai --warm 1 --cwd (string escape --style=url -- "$PWD") --fish-data $__fish_user_data_dir --real-name $__fishsense_real_name
end

function __fishsense_use_real
    set -e __fishsense_pending
    test "$fish_history" = $__fishsense_shadow; or return 0
    set -g fish_history $__fishsense_real_name
end

function __fishsense_use_shadow --description 'point the reader at the ranked session (--merge re-reads the file)'
    if not test -f $__fishsense_shadow_file
        set -g __fishsense_pending 1
        return 1
    end
    set -e __fishsense_pending
    test "$fish_history" = $__fishsense_shadow; or set -g fish_history $__fishsense_shadow
    if test "$argv[1]" = --merge
        builtin history merge
        set -g __fishsense_mtime (path mtime -- $__fishsense_shadow_file)
    end
    return 0
end

function __fishsense_inject --description 'newest shadow item = the suggestion fish shows next'
    builtin history append -- $argv[1]
    set -g __fishsense_mtime (path mtime -- $__fishsense_shadow_file)
    set -ga __fishsense_injected $argv[1]
end

function __fishsense_forget --on-event fish_cancel --description 'ctrl-c: the injected suggestions go, or they win for every prefix they match'
    set -q __fishsense_injected[1]; or return 0
    if test "$fish_history" = $__fishsense_shadow
        for i in $__fishsense_injected
            builtin history delete --exact --case-sensitive -- $i
        end
        builtin history save
        set -g __fishsense_mtime (path mtime -- $__fishsense_shadow_file)
    end
    set -e __fishsense_injected
    return 0
end

function __fishsense_idle --description 'no pager, no history search, nothing holding the real session'
    not set -q __fishsense_sticky; and not commandline --paging-mode; and not commandline --search-mode
end

function __fishsense_trim_cmd --description 'the text fish stores: trailing unescaped spaces/newlines dropped'
    string replace -r '(?<!\\\\)[ \n]+$' '' -- $argv[1] | string collect
end

function __fishsense_keep --description 'would fish keep this line in its history?'
    if functions -q fish_should_add_to_history
        fish_should_add_to_history $argv[1] &>/dev/null
        return
    end
    not string match -q ' *' -- $argv[1]
end

function __fishsense_on_prompt --on-event fish_prompt
    set -e __fishsense_busy __fishsense_armed __fishsense_sticky __fishsense_injected
    if not set -q __fishsense_keymap; or test "$fish_key_bindings" != "$__fishsense_keymap"; or test "$(bind)" != "$__fishsense_bound"
        __fishsense_bind
    end
    __fishsense_on; or return
    set -l name fish
    set -q fish_history[1]; and set name $fish_history[1]
    if test "$name" != $__fishsense_shadow -a "$name" != $__fishsense_real_name
        set -g __fishsense_real_name $name
        __fishsense_rank
    end
    __fishsense_use_shadow --merge
    test -s $__fishsense_result; and __fishsense_take_result --quiet
end

function __fishsense_on_preexec --on-event fish_preexec
    set -g __fishsense_busy 1
    set -g __fishsense_cwd0 $PWD
    set -e __fishsense_armed
    set -g __fishsense_cmd (__fishsense_trim_cmd $argv[1])
    test -n "$__fishsense_cmd"; and __fishsense_keep $argv[1]; or set __fishsense_cmd ''
    string match -qr '^\s*exec\s' -- $argv[1]; and __fishsense_on_exit
    test "$fish_history" = $__fishsense_shadow; or return
    __fishsense_use_real
    test -n "$__fishsense_cmd"; and builtin history append -- $__fishsense_cmd
end

function __fishsense_on_postexec --on-event fish_postexec
    set -l st $status
    __fishsense_on; or return
    set -l cmd $__fishsense_cmd
    set -l log
    if test -n "$cmd"; and not string match -q ' *' -- $cmd
        set log --status $st --log-cwd (string escape --style=url -- "$__fishsense_cwd0") --log-cmd (string escape --style=url -- "$cmd")
        set -g __fishsense_last ''
        test (string length -- "$cmd") -lt 4096; and set -g __fishsense_last $cmd
    end
    __fishsense_rank $log
end

function __fishsense_on_pwd --on-variable PWD
    set -q __fishsense_busy; and return
    status is-command-substitution; and return
    __fishsense_on; and __fishsense_rank
end

function __fishsense_on_switch --on-variable fishsense_disabled --on-variable fish_private_mode
    if __fishsense_on
        __fishsense_rank
    else
        __fishsense_use_real
    end
end

function __fishsense_on_exit --on-event fish_exit
    __fishsense_use_real
    command rm -f -- $__fishsense_shadow_file $__fishsense_result $__fishsense_run/$fish_pid.rank $__fishsense_run/$fish_pid.aireq
end

function __fishsense_on_uninstall --on-event fishsense_uninstall
    __fishsense_unbind
    __fishsense_on_exit
    set -l i (contains -i -- $__fishsense_cache/completions $fish_complete_path); and set -e fish_complete_path[$i]
    functions -e (functions -a -n | string match '__fishsense_*')
    set -e (set -n | string match '__fishsense_*')
    function __fishsense_execute_post
    end
end

function __fishsense_on_signal --on-signal SIGUSR1
    test -s $__fishsense_result; or return
    set -q __fishsense_busy; and return
    if status stack-trace | string match -q '*called on*'
        test $__fishsense_tries -lt 40; or return
        set -g __fishsense_tries (math $__fishsense_tries + 1)
        command sh -c '{ sleep 0.05; kill -USR1 "$0"; } </dev/null >/dev/null 2>&1 &' $fish_pid
        return
    end
    __fishsense_take_result
end

function __fishsense_take_result --description 'apply the helper result; never commandline -r/-i here'
    read -l -d \t kind id payload <$__fishsense_result
    true >$__fishsense_result
    test -n "$kind"; or return
    set -g __fishsense_tries 0
    __fishsense_on; or return
    set payload (string unescape --style=url -- "$payload" | string collect)
    if test "$fish_history" = $__fishsense_shadow
        if test "$kind" = ranked; or test "$(path mtime -- $__fishsense_shadow_file)" != "$__fishsense_mtime"
            __fishsense_use_shadow --merge
        end
    else if set -q __fishsense_pending; and not set -q __fishsense_busy; and __fishsense_idle
        __fishsense_use_shadow --merge
    end
    switch $kind
        case compgen
            string match -q -- "$__fishsense_cache/completions/*.fish" $payload; and test -f $payload; and source $payload
            return
        case ghost
            test "$id" = "$__fishsense_req"; or return
            test "$fish_history" = $__fishsense_shadow; or return
            __fishsense_idle; or return
            commandline --showing-suggestion; and return
            set -l buf "$(commandline)"
            set -l n (string length -- "$buf")
            test $n -gt 0 -a (string length -- "$payload") -gt $n; or return
            test "$(string sub -l $n -- "$payload")" = "$buf"; or return
            __fishsense_inject $payload
    end
    test "$argv[1]" = --quiet; and return
    test "$fish_history" = $__fishsense_shadow; or return
    __fishsense_idle; or return
    test -n "$(commandline)"; and commandline -f undo redo
end

function __fishsense_execute --description 'Enter: autocorrect, then let fish add the line to the real history'
    __fishsense_on; or return 0
    if not commandline --paging-mode; and commandline --is-valid
        __fishsense_correct
    end
    set -g __fishsense_armed 1
    __fishsense_use_real
    return 0
end

function __fishsense_execute_post --description 'nothing ran (pager accept, newline): back to ranked'
    set -q __fishsense_armed; or return 0
    set -e __fishsense_armed
    __fishsense_resume
    return 0
end

function __fishsense_resume --description 'leave the real session once nothing reads it anymore'
    set -q __fishsense_sticky; and not commandline --paging-mode; and not commandline --search-mode
    and set -e __fishsense_sticky
    __fishsense_on; and __fishsense_idle; and test "$fish_history" != $__fishsense_shadow
    and __fishsense_use_shadow
    return 0
end

function __fishsense_hist_begin --description 'history search walks the real, chronological history'
    __fishsense_on; and __fishsense_use_real
    return 0
end

function __fishsense_hist_end
    __fishsense_on; and not set -q __fishsense_sticky; and __fishsense_use_shadow
    return 0
end

function __fishsense_sticky_real --description 'pager or a history-reading function: real until it is done'
    __fishsense_on; or return 0
    set -g __fishsense_sticky 1
    __fishsense_use_real
    return 0
end

function __fishsense_del_begin --description 'shift-delete: what fish deletes from one session goes from the other too'
    set -e __fishsense_del __fishsense_del_before
    __fishsense_on; or return 0
    if commandline --paging-mode; or commandline --search-mode
        set -g __fishsense_del "$(commandline)"
    else if commandline --showing-suggestion; and test "$fish_history" = $__fishsense_shadow
        set -g __fishsense_del ''
        set -g __fishsense_del_before (builtin history search -z --prefix --max 50 -- "$(commandline)" | string split0)
    end
    return 0
end

function __fishsense_del_end
    set -q __fishsense_del; or return 0
    if set -q __fishsense_del_before[1]
        set -l after (builtin history search -z --prefix --max 50 -- "$(commandline)" | string split0)
        for i in (seq (count $__fishsense_del_before))
            test "$__fishsense_del_before[$i]" = "$after[$i]"; and continue
            set __fishsense_del $__fishsense_del_before[$i]
            break
        end
        if test -n "$__fishsense_del"
            builtin history save
            __fishsense_use_real
            builtin history delete --exact --case-sensitive -- $__fishsense_del
            builtin history save
            __fishsense_use_shadow
        end
    else if test -n "$__fishsense_del"; and __fishsense_use_shadow
        builtin history delete --exact --case-sensitive -- $__fishsense_del
        builtin history save
        set -q __fishsense_sticky; and __fishsense_use_real
    end
    set -g __fishsense_mtime (path mtime -- $__fishsense_shadow_file)
    set -e __fishsense_del __fishsense_del_before
    return 0
end

function __fishsense_mark --description 'remember where an accept starts'
    set -g __fishsense_before (string length -- "$(commandline)")
end

function __fishsense_trim --description 'accepting stops at the first placeholder, which is never inserted'
    set -l buf "$(commandline)"
    set -l n 0
    set -q __fishsense_before; and set n $__fishsense_before
    set -e __fishsense_before
    set -l new "$(string sub -s (math $n + 1) -- "$buf")"
    string match -q '*‹*' -- "$new"; or return 0
    if test "$fish_history" = $__fishsense_shadow
        set -g fish_history $__fishsense_real_name
        set -l mine (builtin history search --prefix --case-sensitive --max 1 -- "$buf")
        set -g fish_history $__fishsense_shadow
        set -q mine[1]; and return 0
    end
    set -l keep "$(string sub -l (math $n + (string length -- "$(string replace -r -- '(?s)‹.*' '' "$new")")) -- "$buf")"
    commandline -r -- $keep
    commandline -C (string length -- "$keep")
    return 0
end

function __fishsense_next_async --description 'space: ask for the next parameter in the background'
    __fishsense_on; or return 0
    test "$fish_history" = $__fishsense_shadow; or __fishsense_resume
    commandline --paging-mode; and return 0
    commandline --is-valid; or return 0
    set -l buf "$(commandline)"
    string match -qr '^\S[^\n]*(?<!\\\\) $' -- "$buf"; or return 0
    test (commandline -C) -eq (string length -- "$buf"); or return 0
    set -g __fishsense_req (math $__fishsense_req + 1)
    set -l extra
    set -l cmd (string match -rg '^(\S+) $' -- "$buf")
    if test -n "$cmd"; and not contains -- $cmd $__fishsense_checked; and type -q -- $cmd
        set -ga __fishsense_checked $cmd
        set extra (__fishsense_nocomp $cmd)
    end
    __fishsense_bg next --pid $fish_pid --id $__fishsense_req --cwd (string escape --style=url -- "$PWD") \
        --buffer (string escape --style=url -- "$buf") --fish-data $__fish_user_data_dir \
        --real-name $__fishsense_real_name --notify $extra
    if begin
            not set -q fishsense_ai_autospace; or test "$fishsense_ai_autospace" != 0
        end; and not __fishsense_on_battery
        set -q fishsense_ai_model[1]; and set -lx FISHSENSE_AI_MODEL $fishsense_ai_model[1]
        test -w $__fishsense_run; and true >$__fishsense_run/ai.used
        echo $__fishsense_req >$__fishsense_run/$fish_pid.aireq
        __fishsense_cmd_meta $buf
        __fishsense_bg ai --pid $fish_pid --id $__fishsense_req --cwd (string escape --style=url -- "$PWD") \
            --buffer (string escape --style=url -- "$buf") --fish-data $__fish_user_data_dir \
            --real-name $__fishsense_real_name --cmd-custom $__fsm_custom \
            --cmd-desc (string escape --style=url -- "$__fsm_desc") --notify 1
    end
    return 0
end

function __fishsense_cmd_meta --description 'is the command word a custom one? sets $__fsm_custom and $__fsm_desc'
    set -g __fsm_custom 0
    set -g __fsm_desc ''
    set -l cmd (string match -rg '^\s*([^\s;|&<>()$]+)' -- $argv[1])
    test -n "$cmd"; or return
    if functions -q -- $cmd
        set __fsm_custom 1
        set __fsm_desc (functions -- $cmd | string match -rg "description '?([^']*)'?")[1]
    else
        string match -q "$HOME/*" -- (command -v -- $cmd 2>/dev/null); and set __fsm_custom 1
    end
end

function __fishsense_on_battery --description 'a Mains supply exists and none is online'
    set -l mains
    for d in /sys/class/power_supply/*
        test "$(cat $d/type 2>/dev/null)" = Mains; and set -a mains $d
    end
    set -q mains[1]; or return 1
    for d in $mains
        test "$(cat $d/online 2>/dev/null)" = 1; and return 1
    end
    return 0
end

__fishsense_rank
