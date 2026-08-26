#!/bin/bash

# Extract the inline SQL from Databricks Lakeview dashboards (*.lvdash.json) and run
# sqlfluff over every snippet.
#
# Snippets are written to files in a scratch directory rather than being passed as
# arguments, so a query of any size is handled, and sqlfluff is invoked once per snippet
# kind rather than once per snippet, which keeps a dashboard with fifty widgets fast.
set -u

# Everything is configured through the command line. Under pre-commit that is the only
# channel that works anyway: it does not forward the environment into a `language: docker`
# hook, so a variable set in a shell would apply to a local run and silently not to a
# containerised one.

# Directory holding the extractor and the bundled configs. This script sits next to them
# both in the image (/bin) and in a checkout, so its own directory is the right answer.
bin_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"

dialect=databricks
# lint | off. Applies to dataset queries.
query_mode=lint
# lint | off. Applies to widget field expressions.
expression_mode=lint
# Print which file each snippet came from and where in the json it was found. This is
# debugging output: on a failing run the violations already name their json path, so it
# only gets in the way of the thing you are trying to read.
verbose=0
# Leave the extracted .sql files behind instead of removing them on exit.
keep_tmp=0
# sqlfluff config for dataset queries. Empty means "work it out", see resolve_config.
config=
# sqlfluff config for widget expressions. Empty means the bundled syntax-only one.
expression_config_flag=

status=0

die() {
    printf 'validate-dashboard-sql: %s\n' "$1" >&2
    exit 2
}

# Progress detail, suppressed unless --verbose. Always returns 0 so that using it as the
# last statement of a function does not set that function's exit status.
note() {
    if [ "${verbose}" = "1" ]; then
        # shellcheck disable=SC2059  # the caller supplies the format string
        printf "$@"
    fi
    return 0
}

usage() {
    cat <<'EOF'
Usage: validate-dashboard-sql [options] <dashboard.lvdash.json>...

Options:
  --dialect DIALECT           sqlfluff dialect (default: databricks)
  --config FILE               sqlfluff config for dataset queries
  --expression-config FILE    sqlfluff config for widget field expressions
  --query-mode lint|off       how to check dataset queries (default: lint)
  --expression-mode lint|off  how to check widget expressions (default: lint)
  --sqlfluff-arg ARG          extra flag passed to sqlfluff lint, repeatable
  --keep-tmp                  leave the extracted .sql files behind for inspection
  -v, --verbose               also report which snippet came from where in the json
  -h, --help                  show this help

There are no environment variable equivalents. Under pre-commit the command line is the
only channel that works, because pre-commit does not forward the environment into a
`language: docker` hook, so anything else would apply to a local run and silently not to a
containerised one. Pass options through the hook's args:

  - id: validate-dashboard-sql
    args: [--dialect, sparksql, --expression-mode, 'off']

Note that pre-commit's own -v does not reach this script: pre-commit captures hook output
and shows it only when the hook fails or when -v is given, but the hook cannot tell which
mode it is in. To get the snippet inventory under pre-commit, put --verbose in the hook's
args.
EOF
}

sqlfluff_args=()

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dialect) dialect="${2:?--dialect needs a value}"; shift 2 ;;
        --config) config="${2:?--config needs a value}"; shift 2 ;;
        --expression-config) expression_config_flag="${2:?--expression-config needs a value}"; shift 2 ;;
        --query-mode) query_mode="${2:?--query-mode needs a value}"; shift 2 ;;
        --expression-mode) expression_mode="${2:?--expression-mode needs a value}"; shift 2 ;;
        --sqlfluff-arg) sqlfluff_args+=("${2:?--sqlfluff-arg needs a value}"); shift 2 ;;
        --keep-tmp) keep_tmp=1; shift ;;
        -v | --verbose) verbose=1; shift ;;
        -h | --help) usage; exit 0 ;;
        --) shift; break ;;
        -*) die "unknown option '$1' (try --help)" ;;
        *) break ;;
    esac
done

command -v jq > /dev/null 2>&1 || die "jq is not installed"
command -v sqlfluff > /dev/null 2>&1 || die "sqlfluff is not installed"

extractor="${bin_dir}/extract-sql-snippets.jq"
[ -r "${extractor}" ] || die "extractor not found at ${extractor}"

# sqlfluff resolves config relative to the file it is linting, and the snippets live in a
# scratch directory outside the repository, so the config has to be passed explicitly.
#
# Passing it is not only about picking the right rules. Left to itself sqlfluff still reads
# the working directory's config for core settings but resolves the nested sections against
# the snippet's own path, where there is no config at all. A project that sets
#
#     [tool.sqlfluff.core]
#     templater = "placeholder"
#     [tool.sqlfluff.templater.placeholder]
#     param_regex = '...'
#
# then keeps the templater and loses the parameters, and sqlfluff dies with "No param_regex
# nor param_style was provided to the placeholder templater!". Naming the file on the
# command line keeps the two halves together.
#
# Order: --config, then the repository's own config, then the bundled default. sqlfluff
# reads its settings from either a .sqlfluff or a [tool.sqlfluff.*] section in
# pyproject.toml, so both count as "the repository's own".
resolve_config() {
    if [ -n "${config}" ]; then
        printf '%s\n' "${config}"
    elif [ -f "${PWD}/.sqlfluff" ]; then
        printf '%s\n' "${PWD}/.sqlfluff"
    elif [ -f "${PWD}/pyproject.toml" ] \
        && grep -q '^[[:space:]]*\[tool\.sqlfluff' "${PWD}/pyproject.toml"; then
        # Only when it actually configures sqlfluff: nearly every python project has a
        # pyproject.toml, and treating an unrelated one as the config would quietly drop
        # the bundled defaults.
        printf '%s\n' "${PWD}/pyproject.toml"
    else
        printf '%s\n' "$1"
    fi
}

query_config="$(resolve_config "${bin_dir}/sqlfluff-defaults.cfg")"
# Expressions are fragments, so a repository's own rules would flag every one of them.
# They are always checked with the bundled syntax-only config.
expression_config="${expression_config_flag:-${bin_dir}/sqlfluff-expressions.cfg}"

work="$(mktemp -d)"
if [ "${keep_tmp}" = "1" ]; then
    printf 'Keeping extracted snippets in %s\n' "${work}"
else
    trap 'rm -rf "${work}"' EXIT
fi

# Turn a snippet id into something that survives being used as a filename, keeping the
# tail because that is the part that identifies the widget and the field.
safe_name() {
    printf '%s' "$1" \
        | tr -c 'A-Za-z0-9._-' '_' \
        | tail -c 60
}

# Report a sqlfluff run, mapping the scratch filenames in its output back to the json
# paths the snippets came from. Only the "== [path] FAIL" header lines carry a filename.
relabel() {
    awk -v map="$1" '
        BEGIN {
            while ((getline line < map) > 0) {
                sep = index(line, "\t")
                ids[substr(line, 1, sep - 1)] = substr(line, sep + 1)
            }
        }
        /^== \[/ {
            path = $0
            sub(/^== \[/, "", path)
            sub(/\].*$/, "", path)
            base = path
            sub(/^.*\//, "", base)
            if (base in ids) {
                rest = $0
                sub(/^== \[[^]]*\]/, "", rest)
                print "== [" ids[base] "]" rest
                next
            }
        }
        { print }
    '
}

# Lint every snippet of one kind in a single sqlfluff invocation.
check_kind() {
    local kind="$1" mode="$2" config="$3"
    local dir="${work}/${kind}"

    [ -d "${dir}" ] || return 0
    if [ "${mode}" = "off" ]; then
        printf 'Skipping %s snippets (mode=off)\n' "${kind}"
        return 0
    fi
    if [ "${mode}" != "lint" ]; then
        die "unsupported mode '${mode}' for ${kind} snippets, expected 'lint' or 'off'"
    fi
    [ -r "${config}" ] || die "sqlfluff config not found at ${config}"

    printf 'Linting %s %s snippet(s) with %s\n' \
        "$(find "${dir}" -name '*.sql' | wc -l | tr -d ' ')" "${kind}" "${config}"

    # --ignore-local-config stops sqlfluff searching its default locations on top of the
    # config named here, so the file reported above is genuinely the only one in play. It
    # does not suppress --config. Without it the working directory's config is merged in
    # half way: the snippets sit outside the repository, so core settings are picked up but
    # the nested sections they depend on are not, which is what breaks a project that
    # configures a templater. It also keeps a stray ~/.sqlfluff from changing CI results.
    sqlfluff lint \
        --dialect "${dialect}" \
        --config "${config}" \
        --ignore-local-config \
        ${sqlfluff_args[@]+"${sqlfluff_args[@]}"} \
        "${dir}" 2>&1 | relabel "${work}/map.tsv"

    # PIPESTATUS[0] is sqlfluff's status; the pipe would otherwise report awk's.
    [ "${PIPESTATUS[0]}" -eq 0 ] || status=1
}

# Snippet counter across all dashboards, so scratch filenames stay unique when more than
# one file is checked in the same run.
extracted=0

extract_file() {
    local file="$1" seq kind id name target

    if ! jq -e . "${file}" > /dev/null 2>&1; then
        printf '%s: not valid json\n' "${file}" >&2
        status=1
        return
    fi

    while IFS= read -r record; do
        [ -n "${record}" ] || continue
        seq="$(printf '%s' "${record}" | jq -r '.seq')"
        kind="$(printf '%s' "${record}" | jq -r '.kind')"
        id="$(printf '%s' "${record}" | jq -r '.id')"

        extracted=$((extracted + 1))
        mkdir -p "${work}/${kind}"
        name="$(printf '%04d_%s.sql' "${extracted}" "$(safe_name "${id}")")"
        target="${work}/${kind}/${name}"

        if [ "${kind}" = "expression" ]; then
            # A field expression is not a statement, so wrap it into the smallest query
            # that makes it parsable. Reported positions on line 1 are shifted by the
            # seven characters of "SELECT ".
            printf 'SELECT %s\n' "$(printf '%s' "${record}" | jq -r '.sql')" > "${target}"
        else
            printf '%s\n' "$(printf '%s' "${record}" | jq -r '.sql')" > "${target}"
        fi

        printf '%s\t%s: %s\n' "${name}" "${file}" "${id}" >> "${work}/map.tsv"
        note '  Found #%s %-10s %s\n' "${seq}" "${kind}" "${id}"
    done < <(jq -c --from-file "${extractor}" "${file}")
}

if [ "$#" -eq 0 ]; then
    die "no dashboard files given"
fi

: > "${work}/map.tsv"

for dashboard in "$@"; do
    note 'Extracting SQL from %s\n' "${dashboard}"
    extract_file "${dashboard}"
done

if [ ! -s "${work}/map.tsv" ]; then
    printf 'No SQL snippets found.\n'
    exit "${status}"
fi

check_kind query "${query_mode}" "${query_config}"
check_kind expression "${expression_mode}" "${expression_config}"

exit "${status}"
