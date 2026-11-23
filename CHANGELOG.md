# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-17

### Changed

- Public typed study results serialize their common header, named stable tables, and summary rather than engine-specific payload fields.

### Added

- Python tools for loading network models and running power-system studies.
- AC load-flow, contingency, short-circuit, and dynamic-study capabilities.
- A desktop interface, command-line tools, and an MCP server interface.
- An included IEEE 39-bus case with documented provenance.
- Structured outputs for reproducible studies and result export.
