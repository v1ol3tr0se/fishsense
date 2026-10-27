#!/usr/bin/env bash
usage() {
    cat <<EOT
Usage: backup [options] -d DEST SOURCE

  -d DEST, --dest=DEST   destination directory
  -c, --compress         compress (one of: zstd, gzip, none)
  -q                     quiet
EOT
}
while getopts ":hd:qc" opt; do
    case $opt in
        h) usage ;;
        d) dest=$OPTARG ;;
    esac
done
case "$1" in
    push|pull)
        action=$1 ;;
    -n|--dry-run)
        dry=1 ;;
    -x|--exclude)
        exclude=$2
        shift 2 ;;
    --mode=*)
        mode=${1#*=} ;;
esac
