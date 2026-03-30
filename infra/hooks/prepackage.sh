#!/bin/bash

# This script prepares the infra/tmp folder for deployment.
# It assumes the current working directory is the repository root.

set -e

TMP_DIR="infra/tmp"

# Create infra/tmp or clean it if it already exists
if [ -d "$TMP_DIR" ]; then
    echo "Cleaning existing $TMP_DIR..."
    rm -rf "$TMP_DIR"/*
else
    echo "Creating $TMP_DIR..."
    mkdir -p "$TMP_DIR"
fi

# Copy contents of src into infra/tmp (including hidden files)
echo "Copying src contents to $TMP_DIR..."
cp -r src/. "$TMP_DIR/"

# Copy contents of infra/assets into infra/tmp (overwriting if necessary, including hidden files)
echo "Copying infra/assets contents to $TMP_DIR..."
cp -r infra/assets/. "$TMP_DIR/"

# Merge extra-requirements.txt into requirements.txt
if [ -f "$TMP_DIR/extra-requirements.txt" ]; then
    echo "Merging extra-requirements.txt into requirements.txt..."
    # Append extra-requirements.txt to requirements.txt, avoiding duplicates
    echo "" >> "$TMP_DIR/requirements.txt"
    cat "$TMP_DIR/extra-requirements.txt" >> "$TMP_DIR/requirements.txt"
    # Remove the extra-requirements.txt file after merging
    rm "$TMP_DIR/extra-requirements.txt"
fi

echo "prerestore.sh completed successfully."
