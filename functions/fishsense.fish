function fishsense --description 'Context-aware suggestions, autocorrect and AI for fish'
    set -l model qwen2.5-coder:14b
    set -l battery_model qwen2.5-coder:3b
    set -q fishsense_ai_model[1]; and set model $fishsense_ai_model
    switch "$argv[1]"
        case doctor
            FISHSENSE_AI_MODEL=$model __fishsense_doctor
        case setup-ai
            if not command -q ollama
                echo 'Install ollama first: sudo pacman -S ollama (or ollama-cuda / ollama-rocm to use the GPU)' >&2
                return 1
            end
            echo 'Enabling the ollama service (it will ask for your password)…'
            pkexec systemctl enable --now ollama; or return 1
            for i in (seq 50)
                ollama list >/dev/null 2>&1; and break
                sleep 0.2
            end
            echo "Pulling the model $model…"
            ollama pull $model; or return 1
            if test "$model" != "$battery_model"; and not set -q fishsense_ai_model[1]
                echo "Pulling $battery_model for battery…"
                ollama pull $battery_model; or return 1
            end
            echo 'Done: ctrl+space now uses the AI.'
        case update
            echo 'Downloading the tldr pages…'
            set -l kv (string split -m1 \t -- (__fishsense_helper_call tldr-update)[1])
            if test "$kv[1]" = ok
                echo "Done: "(string unescape --style=url -- $kv[2])" pages."
            else
                echo 'Could not download tldr (are you online?).' >&2
                return 1
            end
        case off
            set -U fishsense_disabled 1
            echo 'fishsense is now off in every terminal. To turn it back on: fishsense on'
        case on
            set -e -g fishsense_disabled
            set -e -U fishsense_disabled
            if set -q fish_private_mode
                echo 'Private mode: fishsense does nothing here.' >&2
                return 1
            end
            if status is-interactive; and not functions -q __fishsense_on_prompt
                if not set -q __fishsense_conf
                    echo 'fishsense cannot load in this terminal (is python3 missing?).' >&2
                    return 1
                end
                source $__fishsense_conf
            end
            echo 'fishsense is on.'
        case status
            if not functions -q __fishsense_on_prompt
                echo 'status: not loaded in this terminal'
            else if set -q fish_private_mode
                echo 'status: inactive (private mode)'
            else if set -q fishsense_disabled
                echo 'status: off (fishsense on turns it on)'
            else
                echo 'status: on'
            end
            if functions -q __fishsense_on_prompt
                if __fishsense_on; and test -f $__fishsense_shadow_file
                    echo 'suggestions: ranked by context'
                else if __fishsense_on
                    echo 'suggestions: plain history (context ranking is not ready yet)'
                end
                echo "history: $__fishsense_real_name"
                echo "wrapped bindings: "(count $__fishsense_binds)
            end
            echo "AI model: $model"
        case '' help -h --help
            echo 'usage: fishsense <command>'
            echo '  doctor     check that everything is in order'
            echo '  setup-ai   install the ollama service and model for ctrl+space'
            echo '  update     download the tldr pages again'
            echo '  off | on   turn fishsense off or on in every terminal'
            echo '  status     show how it is working right now'
        case '*'
            echo "fishsense: unknown command '$argv[1]'. Try: fishsense help" >&2
            return 2
    end
end

function __fishsense_helper_call --description 'helper call that also works when the plugin is not loaded here'
    if functions -q __fishsense_call
        __fishsense_call $argv
    else
        set -l py (status dirname)/../conf.d/fishsense/fishsense.py
        set -q FISHSENSE_HELPER[1]; and set py $FISHSENSE_HELPER
        command python3 -B $py $argv 2>/dev/null
    end
end

function __fishsense_doctor
    echo "fish: $version"
    if command -q python3
        echo 'python3: '(command -s python3)
    else
        echo 'python3: not installed; fishsense needs it' >&2
    end
    if not functions -q __fishsense_on_prompt
        echo 'fishsense: not loaded in this terminal (off, private mode or no python3)'
    else
        __fishsense_on; and echo 'fishsense: on'; or echo 'fishsense: off'
        echo "real history: $__fishsense_real_name"
        echo "wrapped bindings: "(count $__fishsense_binds)
        set -l id fishsense_selftest_$fish_pid
        set -l file $__fish_user_data_dir/$id"_history"
        printf '%s\n' '- cmd: echo fishsense-selftest' '  when: '(math (date +%s) - 5) >$file
        set -l back $fish_history
        set -g fish_history $id
        builtin history merge
        set -l got (builtin history search --prefix --max 1 -- 'echo fishsense-self')
        builtin history append -- 'echo fishsense-selftest-2'
        set -a got (builtin history search --prefix --max 1 -- 'echo fishsense-self')
        if set -q back[1]
            set -g fish_history $back
        else
            set -e -g fish_history
        end
        command rm -f -- $file
        if test "$got[1]" = 'echo fishsense-selftest' -a "$got[2]" = 'echo fishsense-selftest-2'
            echo 'suggestion mechanism: ok'
        else
            echo 'suggestion mechanism: FAILED (this fish version cannot switch history sessions)'
        end
    end
    __fishsense_helper_call doctor
end
