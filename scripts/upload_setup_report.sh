#!/usr/bin/env bash
#
# upload_setup_report.sh — send the secret-free setup report to GitHub.
#
# The report is generated automatically when scripts/setup.py writes the .env
# files. By default this script creates a new issue in the current repository.
# Use --issue N to comment on an existing issue instead.
#
# Usage:
#   ./scripts/upload_setup_report.sh [report.md]
#   ./scripts/upload_setup_report.sh --issue 123
#   ./scripts/upload_setup_report.sh --repo owner/repo --title "Setup report"
#
# Requirements: GitHub CLI (`gh`) authenticated with `gh auth login`.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORT="${REPO_ROOT}/reports/setup-report.md"
ISSUE=""
REPO=""
TITLE=""
DRY_RUN=false

usage() {
    sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --issue)
            ISSUE="${2:-}"
            [[ -n "${ISSUE}" ]] || { echo "ERROR: --issue requires a number" >&2; exit 2; }
            shift 2
            ;;
        --repo)
            REPO="${2:-}"
            [[ -n "${REPO}" ]] || { echo "ERROR: --repo requires owner/repo" >&2; exit 2; }
            shift 2
            ;;
        --title)
            TITLE="${2:-}"
            [[ -n "${TITLE}" ]] || { echo "ERROR: --title requires text" >&2; exit 2; }
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            echo "ERROR: unknown option '$1'" >&2
            usage >&2
            exit 2
            ;;
        *)
            REPORT="$1"
            shift
            ;;
    esac
done

if [[ ! -f "${REPORT}" ]]; then
    echo "ERROR: report not found: ${REPORT}" >&2
    echo "Run 'python3 scripts/setup.py' and save the configuration first." >&2
    exit 1
fi

if [[ ! -s "${REPORT}" ]]; then
    echo "ERROR: report is empty: ${REPORT}" >&2
    exit 1
fi

gh_args=()
repo_hint=""
if [[ -n "${REPO}" ]]; then
    gh_args+=(--repo "${REPO}")
    repo_hint="--repo ${REPO} "
fi

if [[ "${DRY_RUN}" == "true" ]]; then
    echo "Report: ${REPORT}"
    if [[ -n "${ISSUE}" ]]; then
        echo "Would comment on issue ${ISSUE} with: gh issue comment ${ISSUE} ${repo_hint}--body-file ${REPORT}"
    else
        echo "Would create an issue with: gh issue create ${repo_hint}--title '${TITLE:-Setup report <date>}' --body-file ${REPORT}"
    fi
    exit 0
fi

if ! command -v gh >/dev/null 2>&1; then
    echo "ERROR: GitHub CLI 'gh' is not installed." >&2
    echo "Install it from https://cli.github.com/ and run 'gh auth login'." >&2
    exit 1
fi

if [[ -n "${ISSUE}" ]]; then
    gh issue comment "${ISSUE}" "${gh_args[@]}" --body-file "${REPORT}"
else
    title="${TITLE:-Setup report $(date -u +%Y-%m-%d)}"
    gh issue create "${gh_args[@]}" --title "${title}" --body-file "${REPORT}"
fi
