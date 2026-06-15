"""Tests: CredentialStore module"""
import sys; sys.path.insert(0, ".")
import os, shutil
from core.credentials import CredentialStore, mask_secret, sanitize_output, get_credential_store

# Create isolated store
cs = CredentialStore(config_dir="/tmp/test_credstore")

# Set and get
cs.set("deepseek", "api_key", "sk-test12345678901234567890")
cs.set("openai", "api_key", "sk-openai-test-1234567890")

val = cs.get("deepseek", "api_key")
assert val == "sk-test12345678901234567890", f"Got: {val}"

val2 = cs.get("openai", "api_key")
assert val2.startswith("sk-openai"), f"Got: {val2}"

# Get non-existent returns default
val3 = cs.get("nonexistent", "key", "default_val")
assert val3 == "default_val"

# List services
services = cs.list_services()
assert "deepseek" in services
assert "openai" in services

# List keys
keys = cs.list_keys("deepseek")
assert "api_key" in keys

# Delete
assert cs.delete("deepseek", "api_key")
assert cs.get("deepseek", "api_key") == ""

# Mask secret
masked = mask_secret("sk-test1234567890")
assert "****" in masked
assert not "1234567890" in masked[masked.find("****"):]

# Sanitize output
output = "My API key is sk-test12345678901234567890 and my token is ghp_abcd1234567890123456789012345678901234"
cleaned = sanitize_output(output)
assert "****" in cleaned
assert "test1234567890" not in cleaned

# Persistence check
cs2 = CredentialStore(config_dir="/tmp/test_credstore")
assert cs2.get("openai", "api_key").startswith("sk-openai")

# Cleanup
shutil.rmtree("/tmp/test_credstore", ignore_errors=True)

print("ALL CREDENTIAL TESTS PASSED")
