#!/bin/bash
cd "$(dirname "$0")"
echo "=== Agent Record Manager - Running Tests ==="
python3 -m pytest tests/ -v --tb=short 2>/dev/null || python3 -m unittest discover -s tests -v
echo "=== Tests Complete ==="
