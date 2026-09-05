# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versions are derived automatically from git tags (`vX.Y.Z`) via
`uv-dynamic-versioning`.

## [Unreleased]

## [0.1.0rc2] - 2026-09-05

### Added
- Prompt interactively for missing or rejected Forgejo credentials in terminal
  CLI sessions, without persisting passwords.

### Fixed
- Publish GitHub Releases through a draft-first flow so immutable releases
  receive the wheel and source distribution before publication.

## [0.1.0rc1] - 2026-09-04

### Added
- Initial prerelease of the MCP server for managing Forgejo repository projects,
  columns, cards/issues, and milestones through Forgejo's internal web routes.
- Bulk and composed readers with state and resource filters, pagination, request
  throttling, and explicit partial-error reporting.
- A CLI exposing every MCP tool as a subcommand, automatic `.env` loading,
  cached session authentication, configurable logging, and concurrency controls.
- User, contributor, architecture, automation-reference, and versioned MkDocs
  documentation, plus tag-driven PyPI and GitHub release workflows.

<!--
When cutting a release, move the Unreleased items into a new section titled
exactly `## [X.Y.Z] - YYYY-MM-DD` (the release workflow extracts the notes for a
tag `vX.Y.Z` from the matching `## [X.Y.Z]` heading), then tag and push:

    git tag vX.Y.Z && git push origin vX.Y.Z

Example:

## [0.1.0] - 2026-09-04

### Added
- Initial release: MCP server for managing Forgejo Projects/Kanban boards
  (projects, columns, cards, milestones) over Forgejo's internal web routes.
-->
