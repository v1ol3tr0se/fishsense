function upscale --description 'Upscale images'
    argparse -n upscale --min-args 1 h/help 's/scale=' 'f/format=!_validate_int' 'o/output=' 'n-noise=' \
        v/verbose dry-run 'j#jobs' -- $argv
    or return
    if set -q _flag_help
        echo "usage: upscale [-s N] file..."
        return
    end
end

function _upscale_helper
    argparse z/zzz -- $argv
end
