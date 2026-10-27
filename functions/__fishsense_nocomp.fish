function __fishsense_nocomp -a cmd --description 'next args asking for generated completions when fish has none for cmd'
    __fishsense_has_compfile $cmd; and return
    set -l spec (complete -c $cmd)
    set -l kind (type -t -- $cmd 2>/dev/null)
    if set -q spec[1]
        string match -qv -- '* --wraps *' $spec; and return
        set cmd (string match -rg -- ' --wraps (\S+)' $spec[1] | string unescape)
        __fishsense_has_compfile $cmd; and return
        complete -c $cmd | string match -q '*'; and return
        set kind (type -t -- $cmd 2>/dev/null)
        test -n "$kind"; or return
        set kind wraps
    end
    set -l path
    switch "$kind"
        case file
            set path (command -s -- $cmd)
        case function
            set path (functions --details -- $cmd)
        case wraps
            set path (command -s -- $cmd); or set path (functions --details -- $cmd)
        case '*'
            return
    end
    string match -q '/*' -- "$path"; or return
    printf '%s\n' --no-completions --cmd-kind $kind --cmd-path $path --cmd-name $cmd
end

function __fishsense_has_compfile -a cmd --description 'a completion file fish would autoload, on disk or built in'
    path filter -q -- $fish_complete_path/$cmd.fish; or status list-files completions/$cmd.fish &>/dev/null
end
