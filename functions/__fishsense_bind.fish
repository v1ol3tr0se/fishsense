function __fishsense_unbind --description 'undo our wraps that are still in place, giving back what the user had'
    for r in $__fishsense_binds
        set -l f (string split \x1e -- $r)
        bind --user -M $f[1] -- $f[3] 2>/dev/null | string match -q '*__fishsense_*'; or continue
        bind -M $f[1] -e -- $f[3]
        if test "$f[4]" = user
            set -l sm
            test -n "$f[2]"; and set sm -m $f[2]
            bind -M $f[1] $sm -- $f[3] $f[5..]
        end
    end
    set -g __fishsense_binds
    set -l orig (set -n | string match '__fishsense_ai_orig_*')
    set -q orig[1]; and set -e $orig
end

function __fishsense_bind --description 'wrap the effective binding of every mode with the fishsense hooks; idempotent'
    set -g __fishsense_keymap "$fish_key_bindings"
    __fishsense_unbind

    set -l lines (bind | string match -- 'bind *')
    set -l user (string match -v -- 'bind --preset *' $lines)
    set -l preset (string match -- 'bind --preset *' $lines)
    set -l readers
    set -l pagers
    set -l rl (bind --function-names)
    for fn in (string match -ra -- '[A-Za-z_][\w-]*' $user $preset | path sort -u)
        string match -q '__fishsense_*' -- $fn; or contains -- $fn $rl; or not functions -q -- $fn; and continue
        set -l body (functions -- $fn)
        string match -q '*history*' -- $body; or continue
        set -a readers $fn
        string match -q '*history-pager*' -- $body; and set -a pagers $fn
    end
    set -l re '\b(?:execute|up-or-search|down-or-search|history-[a-z-]+|beginning-of-history|end-of-history|accept-autosuggestion|forward-(?:single-)?char|end-of-line|forward-(?:big)?word(?:-vi|-end)?|nextd-or-forward-word|forward-token|forward-path-component)\b|^bind --preset (?:-M \S+ )?(?:-m \S+ )?(?:space|ctrl-space) '
    set -l word '(?:^|[\s;|&(\'"])(?:'(string join '|' -- $readers)')(?=$|[\s;|&)\'"])'
    set -q readers[1]; and set re "$re|$word"
    set preset (string match -re -- $re $preset)
    set preset (string match -rv -- '^bind --preset -M (?!default |insert )\S+ (?!.*(execute|history|search))' $preset)
    set -l reader_re '^$'
    set -q readers[1]; and set reader_re $word
    set -l pager_re '^$'
    set -q pagers[1]; and set pager_re '(?:^|[\s;|&(])(?:'(string join '|' -- $pagers)')(?=$|[\s;|&)])'

    set -l seen
    set -l ai_modes
    for line in $user $preset
        set -l t
        if string match -qr '[\\\\\'"]' -- $line
            printf '%s\n' $line | read -at t
        else
            set t (string split ' ' -- $line)
        end
        set -l origin user
        set -l mode default
        set -l sm
        set -e t[1]
        while set -q t[1]
            switch $t[1]
                case --preset
                    set origin preset
                case -M --mode
                    set mode $t[2]
                    set -e t[1]
                case -m --sets-mode
                    set sm $t[2]
                    set -e t[1]
                case --
                    set -e t[1]
                    break
                case '-*'
                case '*'
                    break
            end
            set -e t[1]
        end
        set -q t[2]; or continue
        set -l key $t[1]
        set -l cmds $t[2..]
        contains -- "$mode $key" $seen; and continue
        set -a seen "$mode $key"
        string match -q '*__fishsense_*' -- $cmds; and continue

        set -l pre
        set -l post
        set -l new
        set -l words (string split ' ' -- $cmds)
        if test "$key" = ctrl-space; and contains -- $mode default insert
            set new __fishsense_ai
            set -a ai_modes $mode
            set -g __fishsense_ai_orig_$mode $cmds
        else
            if contains -- execute $words
                set pre __fishsense_execute
                set post __fishsense_execute_post
            end
            if contains -- history-pager $words; or string match -qr -- $pager_re $cmds
                set -a pre __fishsense_sticky_real
            else if string match -qr -- $reader_re $cmds
                or string match -qr -- '^(up-or-search|down-or-search|history-(prefix-|token-|last-token-)?search-(backward|forward)|beginning-of-history|end-of-history)$' $words
                set -a pre __fishsense_hist_begin
                set -a post __fishsense_hist_end
            end
            if string match -qr -- '^history-(pager-)?delete$' $words
                set -a pre __fishsense_del_begin
                set -a post __fishsense_del_end
            end
            if string match -qr -- '^(accept-autosuggestion|forward-(single-)?char|end-of-line|forward-(big)?word(-vi|-end)?|nextd-or-forward-word|forward-token|forward-path-component)$' $words
                set -a pre __fishsense_mark begin-undo-group
                set -a post __fishsense_trim end-undo-group
            end
            test "$key" = space; and contains -- self-insert $words; and set -a post __fishsense_next_async
            set -q pre[1]; or set -q post[1]; or continue
            set new $pre $cmds $post
            if contains -- and $cmds; or contains -- or $cmds
                set -l i (math "min($(string join , (contains -i -- and $cmds) (contains -i -- or $cmds)))")
                test $i -gt 1; and set new $pre $cmds[1..(math $i - 1)] $post $cmds[$i..]
            end
        end
        set -l smf
        test -n "$sm"; and set smf -m $sm
        bind -M $mode $smf -- $key $new 2>/dev/null; or continue
        set -a __fishsense_binds (string join \x1e -- $mode "$sm" $key $origin $cmds)
    end
    for mode in default insert
        contains -- $mode $ai_modes; or contains -- "$mode ctrl-space" $seen; and continue
        bind -M $mode ctrl-space __fishsense_ai
        set -a __fishsense_binds (string join \x1e -- $mode '' ctrl-space preset)
    end
    for mode in default insert
        contains -- "$mode ctrl-shift-space" $seen; and continue
        bind -M $mode ctrl-shift-space '__fishsense_ai local' 2>/dev/null
        and set -a __fishsense_binds (string join \x1e -- $mode '' ctrl-shift-space preset)
    end
    set -g __fishsense_bound "$(bind)"
end
