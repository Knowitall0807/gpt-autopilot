#!/usr/bin/env python3

import openai
import json
import os
import traceback
import sys
import shutil
import re
import time
import random
import copy

import gpt_functions
from helpers import yesno, safepath
import chatgpt
import betterprompter
from config import get_config, save_config

CONFIG = get_config()

def compact_commands(messages):
    for msg in messages:
        if msg["role"] == "function" and msg["name"] == "write_file":
            msg["content"] = "Respond with file content. End with END_OF_OUTPUT"
    return messages

def remove_hallucinations(messages):
    for msg in messages:
        if msg["role"] == "function" and msg["name"] == "write_file":
            try:
                args = json.loads(msg["function_call"]["arguments"])
                if "content" in args:
                    args.pop("content")
                    msg["function_call"]["arguments"] = json.dumps(args)
            except:
                continue
    return messages

def actually_write_file(filename, content):
    filename = safepath(filename)

    # detect partial file content response
    if "END_OF_OUTPUT" not in content:
        print(f"ERROR: Partial write response for code/{filename}...")
        return "ERROR: No END_OF_OUTPUT detected"

    parts = re.split("```[\w]+?\n", content + "\n")
    if len(parts) > 1:
        if parts[0] != "":
            print("ERROR: Unexpected text before code block")
            return "ERROR: Unexpected text before code block"
        content = parts[1]

    parts = content.split("END_OF_OUTPUT")
    content = parts[0]

    # trim whitespace and ticks
    content = content.strip().strip("`")

    # force newline in the end
    if content[-1] != "\n":
        content = content + "\n"

    # Create parent directories if they don't exist
    parent_dir = os.path.dirname(f"code/{filename}")
    os.makedirs(parent_dir, exist_ok=True)

    if os.path.isdir(f"code/{filename}"):
        return "ERROR: There is already a directory with this name"

    with open(f"code/{filename}", "w") as f:
        f.write(content)

    print(f"Wrote to file code/{filename}...")
    return f"File {filename} written successfully"

def ask_model_switch():
    if yesno("ERROR: You don't seem to have access to the GPT-4 API. Would you like to change to GPT-3.5?") == "y":
        CONFIG["model"] = "gpt-3.5-turbo-0613"
        save_config(CONFIG)
        return CONFIG["model"]
    else:
        sys.exit(1)

# MAIN FUNCTION
def run_conversation(prompt, model = "gpt-4-0613", messages = [], conv_id = None, recursive = True, temp = 1.0):
    if conv_id is None:
        conv_id = str(sum(1 for entry in os.scandir("history"))).zfill(4)

    if messages == []:
        with open("system_message", "r") as f:
            system_message = f.read()

        # add system message
        messages.append({
            "role": "system",
            "content": system_message
        })

        # add list of current files to user prompt
        prompt += "\n\n" + gpt_functions.list_files()

    # add user prompt to chatgpt messages
    try:
        messages = chatgpt.send_message(
            message={
                "role": "user",
                "content": prompt
            },
            messages=messages,
            model=model,
            conv_id=conv_id,
            temp=temp,
        )
    except Exception as e:
        if "The model: `gpt-4-0613` does not exist" in str(e):
            model = ask_model_switch()
        else:
            raise

    # get chatgpt response
    message = messages[-1]

    mode = None
    filename = None
    function_call = "auto"
    print_message = True

    # loop until project is finished
    while True:
        if message.get("function_call"):
            # get function name and arguments
            function_name = message["function_call"]["name"]
            arguments_plain = message["function_call"]["arguments"]
            arguments = None

            try:
                # try to parse arguments
                arguments = json.loads(arguments_plain)

            # if parsing fails, try to fix format
            except:
                try:
                    # gpt-3.5 sometimes uses backticks
                    # instead of double quotes in JSON value
                    print("ERROR: Invalid JSON arguments. Fixing...")
                    arguments_fixed = arguments_plain.replace("`", '"')
                    arguments = json.loads(arguments_fixed)
                except:
                    try:
                        # gpt-3.5 sometimes omits single quotes
                        # from around keys
                        print("ERROR: Invalid JSON arguments. Fixing again...")
                        arguments_fixed = re.sub(r'(\b\w+\b)(?=\s*:)', r'"\1"')
                        arguments = json.loads(arguments_fixed)
                    except:
                        try:
                            # gpt-3.5 sometimes uses single quotes
                            # around keys, instead of double quotes
                            print("ERROR: Invalid JSON arguments. Fixing third time...")
                            arguments_fixed = re.sub(r"'(\b\w+\b)'(?=\s*:)", r'"\1"')
                            arguments = json.loads(arguments_fixed)
                        except:
                            print("ERROR PARSING ARGUMENTS:\n---\n")
                            print(arguments_plain)
                            print("\n---\n")

                            if function_name == "replace_text":
                                function_response = "ERROR! Please try to replace a shorter text or try another method"
                            else:
                                function_response = "Error parsing arguments. Make sure to use properly formatted JSON, with double quotes. If this error persist, change tactics"

            if arguments is not None:
                # call the function given by chatgpt
                if hasattr(gpt_functions, function_name):
                    try:
                        function_response = getattr(gpt_functions, function_name)(**arguments)
                    except TypeError:
                        function_response = "ERROR: Invalid function parameters"
                else:
                    print(f"NOTICE: GPT called function '{function_name}' that doesn't exist.")
                    function_response = f"Function '{function_name}' does not exist."

                if function_name == "write_file":
                    mode = "WRITE_FILE"
                    filename = arguments["filename"]
                    function_call = "none"
                    print_message = False

            messages = remove_hallucinations(messages)

            # if function returns PROJECT_FINISHED, exit
            if function_response == "PROJECT_FINISHED":
                print("## Project finished! ##")

                if recursive == False:
                    return

                next_message = yesno("Do you want to ask something else?\nAnswer", ["y", "n"])
                if next_message == "y":
                    prompt = input("What do you want to ask?\nAnswer: ")
                    return run_conversation(
                        prompt=prompt,
                        model=model,
                        messages=messages,
                        conv_id=conv_id,
                        recursive=recursive,
                    )
                else:
                    sys.exit(0)

            # send function result to chatgpt
            messages = chatgpt.send_message(
                message={
                    "role": "function",
                    "name": function_name,
                    "content": function_response,
                },
                messages=messages,
                model=model,
                function_call=function_call,
                print_message=print_message,
                conv_id=conv_id,
                temp=temp,
            )
        else:
            if mode == "WRITE_FILE":
                user_message = actually_write_file(filename, message["content"])

                if "ERROR" not in user_message:
                    mode = None
                    filename = None
                    function_call = "auto"
                    print_message = True

                messages = compact_commands(messages)
            else:
                if len(message["content"]) > 600:
                    user_message = "ERROR: Please use function calls"
                # if chatgpt doesn't respond with a function call, ask user for input
                elif "?" in message["content"]:
                    user_message = input("ChatGPT didn't respond with a function. What do you want to say?\nAnswer: ")
                else:
                    # if chatgpt doesn't ask a question, continue
                    user_message = "Ok, continue."

            # send user message to chatgpt
            messages = chatgpt.send_message(
                message={
                    "role": "user",
                    "content": user_message,
                },
                messages=messages,
                model=model,
                conv_id=conv_id,
                print_message=print_message,
                temp=temp,
            )

        # save last response for the while loop
        message = messages[-1]

def make_prompt_better(prompt, ask=True):
    print("Making prompt better...")

    try:
        better_prompt = betterprompter.make_better(prompt, CONFIG["model"])
    except Exception as e:
        better_prompt = prompt
        if "The model: `gpt-4-0613` does not exist" in str(e):
            ask_model_switch()
            return make_prompt_better(prompt, ask)
        elif yesno("Unable to make prompt better. Try again?") == "y":
            return make_prompt_better(prompt, ask)
        else:
            return prompt

    if prompt != better_prompt:
        print("## Better prompt: ##\n" + better_prompt)

        if ask == False or yesno("Do you want to use this prompt?") == "y":
            prompt = better_prompt

    return prompt

def reset_code_folder():
    shutil.rmtree("code")
    os.mkdir("code")

def parse_arguments(argv):
    arguments = {
        "program_name": sys.argv.pop(0)
    }

    while sys.argv != []:
        arg_name = sys.argv.pop(0)

        # conversation id
        if arg_name == "--conv":
            if sys.argv == []:
                print(f"ERROR: Missing argument for '{arg_name}'")
                sys.exit(1)
            arguments["conv"] = sys.argv.pop(0)
        # initial prompt
        elif arg_name == "--prompt":
            if sys.argv == []:
                print(f"ERROR: Missing argument for '{arg_name}'")
                sys.exit(1)
            arguments["prompt"] = sys.argv.pop(0)
        # temperature
        elif arg_name == "--temp":
            if sys.argv == []:
                print(f"ERROR: Missing argument for '{arg_name}'")
                sys.exit(1)
            arguments["temp"] = float(sys.argv.pop(0))
        # make prompt better with GPT
        elif arg_name == "--better":
            if "versions" in arguments:
                print("ERROR: --version must come after --better")
                sys.exit(1)
            arguments["better"] = True
        # don't make prompt better with GPT
        elif arg_name == "--not-better":
            arguments["not-better"] = False
        # confirm if user wants to use bettered prompt
        elif arg_name == "--ask-better":
            arguments["ask-better"] = False
        # make a new better prompt for every version
        elif arg_name == "--better-versions":
            arguments["better-versions"] = True
            arguments["better"] = True
        # delete code folder contents before starting
        elif arg_name == "--delete":
            reset_code_folder()
        # make multiple versions of project
        elif arg_name == "--versions":
            if "ask-better" in arguments:
                print(f"ERROR: --ask-better flag is not compatible with --versions flag")
                sys.exit(1)
            if "better" not in arguments:
                arguments["not-better"] = True
            if sys.argv == []:
                print(f"ERROR: Missing argument for '{arg_name}'")
                sys.exit(1)
            arguments["versions"] = int(sys.argv.pop(0))
        else:
            print(f"ERROR: Invalid option '{arg_name}'")
            sys.exit(1)

    if "not-better" in arguments and "better" in arguments:
        print("ERROR: --not-better is not compatible with --better")
        sys.exit(1)

    return arguments

def load_message_history(arguments):
    if "conv" in arguments:
        history_file = arguments["conv"]
        try:
            with open(f"history/{history_file}.json", "r") as f:
                messages = json.load(f)
            print(f"Loaded message history from {history_file}.json")
        except:
            print(f"ERROR: History file {history_file}.json not found")
            sys.exit(1)
    else:
        messages = []

    return messages

def get_api_key():
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key in [None, ""]:
        if "api_key" in CONFIG:
            api_key = CONFIG["api_key"]
        else:
            print("Put your OpenAI API key into the config.json file or OPENAI_API_KEY environment variable to skip this prompt.\n")
            api_key = input("Input OpenAI API key: ").strip()

            if api_key == "":
                sys.exit(1)

            save = yesno("Do you want to save this key to config.json?", ["y", "n"])
            if save == "y":
                CONFIG["api_key"] = api_key
                save_config(CONFIG)
            print()
    return api_key

def warn_existing_code():
    if os.path.isdir("code") and len(os.listdir("code")) != 0:
        answer = yesno("WARNING! There is already some code in the `code/` folder. GPT-AutoPilot may base the project on these files and has write access to them and might modify or delete them.\n\n" + gpt_functions.list_files("", False) + "\n\nDo you want to continue?", ["YES", "NO", "DELETE"])
        if answer == "DELETE":
            reset_code_folder()
        elif answer != "YES":
            sys.exit(0)

def create_directories():
    dirs = ["code", "history"]
    for directory in dirs:
        if not os.path.isdir(directory):
            os.mkdir(directory)

def get_temp(arguments):
    if "temp" in arguments:
        return arguments["temp"]
    return 1.0

def maybe_make_prompt_better(prompt, args, version_loop = False):
    if version_loop == True and "better-versions" not in args:
        return prompt
    if "not-better" not in args:
        if "better" in args or yesno("Do you want GPT to make your prompt better?") == "y":
            ask = "better" not in args or "ask-better" in args
            prompt = make_prompt_better(prompt, ask)
    return prompt

# LOAD COMMAND LINE ARGUMENTS
args = parse_arguments(sys.argv)

# LOAD MESSAGE HISTORY
messages = load_message_history(args)

# GET API KEY
openai.api_key = get_api_key()

# WARN IF THERE IS CODE ALREADY IN THE PROJECT
warn_existing_code()

# CREATE DATA DIRECTORIES
create_directories()

# GET TEMPERATURE
temp = get_temp(args)
temp_orig = temp

# ASK FOR PROMPT
if "prompt" in args:
    prompt = args["prompt"]
else:
    prompt = input("What would you like me to do?\nAnswer: ")

timestamp = int(time.time())

if "versions" in args:
    versions = args["versions"]
    print(f"Creating {versions} versions...")
else:
    versions = 1

if versions > 1:
    if not os.path.isdir("versions"):
        os.mkdir("versions")
    shutil.copytree("code", f"versions/code_{timestamp}_orig")
    recursive = False
else:
    recursive = True

version_folders = []
orig_messages = copy.deepcopy(messages)

for version in range(1, versions+1):
    # reset message history for every version
    messages = copy.deepcopy(orig_messages)

    if versions > 1:
        print(f"\n## VERSION {version} (temp: {temp}) ##")

    # MAKE PROMPT BETTER
    version_loop = version > 1
    prompt = maybe_make_prompt_better(prompt, args, version_loop)

    if version != 1:
        # randomize temperature for every version
        temp = round( temp_orig + random.uniform(0, 0.3), 2 )

        # always start with original version
        shutil.copytree(f"versions/code_{timestamp}_orig", "code")

    # RUN CONVERSATION
    run_conversation(
        prompt=prompt,
        model=CONFIG["model"],
        messages=messages,
        recursive=recursive,
        temp=temp,
    )

    if versions > 1:
        version_folder = f"versions/code_{timestamp}_v{version}"
        shutil.copytree("code", version_folder)
        shutil.rmtree("code")
        version_folders.append(version_folder)

if versions > 1:
    print("\n## ALL VERSIONS FINISHED ##")
    print("You can find all versions here:")
    for number, verfolder in enumerate(version_folders):
        print(f"- Version {number+1}: {verfolder}")
