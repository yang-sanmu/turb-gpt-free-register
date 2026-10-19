"""GptAccount temporary Chrome bridge configuration."""
from config.env_loader import apply_env_overrides

# Empty means the GptAccount directory beside this project.
GPTACCOUNT_PROJECT_DIR = ""
GPTACCOUNT_NODE = "node"
GPTACCOUNT_HEADLESS = False
GPTACCOUNT_LOCALE = "en-US"
GPTACCOUNT_TIMEZONE = ""
GPTACCOUNT_START_TIMEOUT = 75

apply_env_overrides(globals(), {
    "GPTACCOUNT_PROJECT_DIR": "str", "GPTACCOUNT_NODE": "str",
    "GPTACCOUNT_HEADLESS": "bool", "GPTACCOUNT_LOCALE": "str",
    "GPTACCOUNT_TIMEZONE": "str", "GPTACCOUNT_START_TIMEOUT": "int",
})
