#!/bin/bash
set -euo pipefail

REAL_GIT="${CENTAUR_REAL_GIT:-/usr/bin/git}"
CACHE_URL="${CENTAUR_GIT_CACHE_URL:-}"

find_git_command() {
    local expect_value=0
    for arg in "$@"; do
        if [ "$expect_value" = "1" ]; then
            expect_value=0
            continue
        fi
        case "$arg" in
            -C|-c|--git-dir|--work-tree|--namespace|--exec-path|--config-env)
                expect_value=1
                continue
                ;;
            --git-dir=*|--work-tree=*|--namespace=*|--exec-path=*|--config-env=*)
                continue
                ;;
            --paginate|--no-pager|--bare|--version|--help|--html-path|--man-path|--info-path|--literal-pathspecs|--no-literal-pathspecs|--glob-pathspecs|--noglob-pathspecs|--icase-pathspecs)
                continue
                ;;
            --*)
                continue
                ;;
            -* )
                continue
                ;;
            *)
                printf '%s\n' "$arg"
                return 0
                ;;
        esac
    done
    return 1
}

should_use_cache() {
    case "$1" in
        clone|fetch|pull|ls-remote|submodule)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

cmd="$(find_git_command "$@" || true)"
if [ -n "$CACHE_URL" ] && [ -n "$cmd" ] && should_use_cache "$cmd"; then
    CACHE_URL="${CACHE_URL%/}"
    exec "$REAL_GIT" \
        -c "url.${CACHE_URL}/github.com/.insteadOf=https://github.com/" \
        -c "url.${CACHE_URL}/github.com/.insteadOf=git@github.com:" \
        -c "url.${CACHE_URL}/github.com/.insteadOf=ssh://git@github.com/" \
        "$@"
fi

exec "$REAL_GIT" "$@"
