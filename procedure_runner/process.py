import os
import sys
import yaml
import click
import subprocess
from typing import Dict, List, Any, Callable, Optional
import re

safe_builtins = {
    "print": print,
    "len": len,
    "quit": quit
}

class ProcessExecutor:
    """Executes processes defined in YAML files."""
    
    def __init__(self, process_file: str):
        """Initialize with the path to a process YAML file."""
        self.process_file = process_file
        self.process_def = self._load_process()
        self.context = {}  # Stores variables during execution
    
    def _load_process(self) -> Dict[str, Any]:
        """Load and validate the process definition from YAML."""
        try:
            with open(self.process_file, 'r') as f:
                process = yaml.safe_load(f)
            
            # Basic validation
            required_keys = ['name', 'description', 'steps']
            for key in required_keys:
                if key not in process:
                    raise ValueError(f"Missing required key '{key}' in process definition")
            
            # Validate each step has required keys
            for i, step in enumerate(process['steps']):
                if 'type' not in step:
                    raise ValueError(f"Step {i} missing required 'type' field")
                if 'id' not in step:
                    raise ValueError(f"Step {i} missing required 'id' field")
            
            return process
        except Exception as e:
            click.echo(f"Error loading process definition: {e}", err=True)
            sys.exit(1)
    
    def execute(self) -> None:
        """Execute the process step by step."""
        process = self.process_def
        click.secho(f"Starting process: {process['name']}", fg='green', bold=True)
        click.echo(f"{process['description']}\n")
        
        # Execute each step in order
        for step_num, step in enumerate(process['steps'], 1):
            step_type = step['type']
            step_id = step['id']
            
            # Check if we should skip this step based on condition
            if 'run_if' in step:
                condition = step['run_if']
                # Replace variables in condition
                for var_name, var_value in self.context.items():
                    condition = condition.replace(f"${{{var_name}}}", f"'{str(var_value)}'")
                
                try:
                    should_run = eval(condition, {"__builtins__": safe_builtins}, {})
                    if not should_run:
                        click.secho(f"Skipping step {step_num}: {step.get('name', step_id)} (condition not met)", fg='yellow')
                        continue
                except Exception as e:
                    click.secho(f"Error evaluating run_if condition: {e}", fg='red')
                    if step.get('exit_on_failure', True):
                        sys.exit(1)
            
            click.secho(f"Step {step_num}: {step.get('name', step_id)}", fg='blue', bold=True)
            if 'description' in step:
                click.echo(f"{step['description']}")
            
            # Dispatch to the appropriate step handler
            if step_type == 'input':
                self._handle_input_step(step)
            elif step_type == 'command':
                self._handle_command_step(step)
            elif step_type == 'validation':
                self._handle_validation_step(step)
            elif step_type == 'choice':
                self._handle_choice_step(step)
            elif step_type == 'file_check':
                self._handle_file_check_step(step)
            else:
                click.echo(f"Unknown step type: {step_type}", err=True)
                if not click.confirm("Continue anyway?", default=False):
                    sys.exit(1)
            
            click.echo()  # Add a blank line between steps
        
        click.secho("Process completed successfully!", fg='green', bold=True)
    

    def _handle_input_step(self, step: Dict[str, Any]) -> None:
        """Handle an input step that collects information from the user."""
        prompt = step.get('prompt', f"Enter {step['id']}")
        default = step.get('default', None)
        required = step.get('required', True)

        # Replace variables in prompt
        for var_name, var_value in self.context.items():
            prompt = prompt.replace(f"${{{var_name}}}", str(var_value))

        while True:
            value = click.prompt(prompt, default=default, show_default=True)
            
            # Apply validation if specified
            if 'validation' in step:
                pattern = step['validation'].get('pattern')
                if pattern and not re.match(pattern, value):
                    error_msg = step['validation'].get('error', f"Invalid input. Must match pattern: {pattern}")
                    click.secho(error_msg, fg='red')
                    continue
            
            # Store the value in context
            self.context[step['id']] = value
            break
    
    def _handle_command_step(self, step: Dict[str, Any]) -> None:
        """Execute a shell command and optionally capture its output."""
        command = step['command']
        
        # Replace variables in command & prompt
        for var_name, var_value in self.context.items():
            command = command.replace(f"${{{var_name}}}", str(var_value))
        
        click.echo(f"Executing: {command}")
        
        # Always capture output for analysis but honor show_output flag for display
        show_output = step.get('show_output', True)
        exit_on_error = step.get('exit_on_error', True)
        
        try:
            result = subprocess.run(
                command,
                shell=True,
                check=False,  # We'll handle errors manually for more control
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()
            
            # Store outputs
            if 'output_var' in step:
                self.context[step['output_var']] = stdout
            
            if 'error_var' in step:
                self.context[step['error_var']] = stderr
            
            # Display outputs if requested
            if stdout and show_output:
                click.echo(stdout)
            
            if stderr and show_output:
                click.secho(stderr, fg='yellow')
            
            # Handle regex pattern matching for success/failure
            success = True
            
            # Check for pass_on_match patterns (success if any match)
            if 'pass_on_match' in step:
                patterns = step['pass_on_match'] if isinstance(step['pass_on_match'], list) else [step['pass_on_match']]
                success = any(re.search(pattern, stdout + "\n" + stderr) for pattern in patterns)
                
                if success:
                    click.secho("✓ Output matched success pattern", fg='green')
                else:
                    click.secho("✗ Output didn't match any success pattern", fg='red')
            
            # Check for fail_on_match patterns (failure if any match)
            if 'fail_on_match' in step:
                patterns = step['fail_on_match'] if isinstance(step['fail_on_match'], list) else [step['fail_on_match']]
                if any(re.search(pattern, stdout + "\n" + stderr) for pattern in patterns):
                    click.secho("✗ Output matched failure pattern", fg='red')
                    success = False
            
            # Check exit code if we should still care about it
            if exit_on_error and result.returncode != 0 and success:
                success = False
                click.secho(f"Command failed with exit code {result.returncode}", fg='red')
            
            # Store the command result in context
            self.context[f"{step.get('id', 'command')}_success"] = success
            
            # Exit if failure and exit_on_error
            if not success and exit_on_error:
                sys.exit(result.returncode if result.returncode != 0 else 1)
                
        except Exception as e:
            click.secho(f"Error executing command: {e}", fg='red')
            if exit_on_error:
                sys.exit(1)
    
    def _handle_validation_step(self, step: Dict[str, Any]) -> None:
        """Validate a condition and either continue or exit."""
        condition = step['condition']
        
        # Replace variables in condition
        for var_name, var_value in self.context.items():
            condition = condition.replace(f"${{{var_name}}}", f"'{str(var_value)}'")
        
        try:
            # Safely evaluate the condition
            result = eval(condition, {"__builtins__": safe_builtins}, {})
            
            if result:
                click.secho("Validation passed!", fg='green')
            else:
                click.secho(f"Validation failed: {step.get('error', 'Condition not met')}", fg='red')
                if step.get('exit_on_failure', True):
                    sys.exit(1)
        except Exception as e:
            click.secho(f"Error evaluating condition: {e}", fg='red')
            if step.get('exit_on_failure', True):
                sys.exit(1)
    
    def _handle_choice_step(self, step: Dict[str, Any]) -> None:
        """Present a set of choices to the user."""
        choice_options = step['choices']
        default = step.get('default')
        
        prompt = step.get('prompt', "Select an option")
        # Replace variables in prompt
        for var_name, var_value in self.context.items():
            prompt = prompt.replace(f"${{{var_name}}}", str(var_value))

        # Build choice display
        choice_text = []
        for idx, choice in enumerate(choice_options, 1):
            choice_text.append(f"{idx}. {choice['name']}")
        
        click.echo("\n".join(choice_text))
        
        # Get user choice
        while True:
            value = click.prompt(
                prompt,
                default=default,
                show_default=True
            )
            
            try:
                choice_idx = int(value) - 1
                if 0 <= choice_idx < len(choice_options):
                    selected = choice_options[choice_idx]
                    click.echo(f"Selected: {selected['name']}")
                    
                    # Store the selected value
                    self.context[step['id']] = selected.get('value', selected['name'])
                    
                    # Execute actions for this choice if any
                    if 'action' in selected:
                        self._handle_command_step({'command': selected['action'], 'type': 'command'})
                    if 'action_python' in selected:
                        try:
                            exec(selected['action_python'], {"__builtins__": safe_builtins}, self.context)
                        except Exception as e:
                            click.secho(f"Error executing Python action: {e}", fg='red')
                            if step.get('exit_on_failure', True):
                                sys.exit(1)
                    break
                else:
                    click.secho(f"Invalid choice. Please enter a number between 1 and {len(choice_options)}", fg='red')
            except ValueError:
                click.secho("Please enter a number", fg='red')
    
    def _handle_file_check_step(self, step: Dict[str, Any]) -> None:
        """Check if a file exists and take appropriate action."""
        filename = step['filename']
        
        # Replace variables in filename
        for var_name, var_value in self.context.items():
            filename = filename.replace(f"${{{var_name}}}", str(var_value))
        
        exists = os.path.exists(filename)
        
        if exists:
            click.secho(f"File '{filename}' exists", fg='green')
            if 'exists_action' in step:
                self._handle_command_step({'command': step['exists_action'], 'type': 'command'})
        else:
            click.secho(f"File '{filename}' does not exist", fg='yellow')
            if 'missing_action' in step:
                self._handle_command_step({'command': step['missing_action'], 'type': 'command'})
            
            if step.get('required', False):
                click.secho("Required file is missing", fg='red')
                if step.get('exit_on_missing', True):
                    sys.exit(1)


@click.group()
def cli():
    """CLI tool for executing guided processes defined in YAML files."""
    pass


@cli.command()
@click.argument('process_file', type=click.Path(exists=False))
def run(process_file):
    """Execute a guided process defined in a YAML file."""

    # Try to find the process file
    if os.path.exists(process_file):
        # User provided a valid direct path
        process_file = process_file
    else:
        # Check if it's in the process directory
        process_dir = os.environ.get('PROCEDURE_DIR', './procedures')
        potential_path = os.path.join(process_dir, process_file)
        
        # Also try with .yml and .yaml extensions if not specified
        if not (process_file.endswith('.yml') or process_file.endswith('.yaml')):
            for ext in ['.yml', '.yaml']:
                if os.path.exists(potential_path + ext):
                    potential_path = potential_path + ext
                    break
        
        if os.path.exists(potential_path):
            process_file = potential_path
        else:
            click.secho(f"Process file not found: {process_file}", fg="red")
            click.echo(f"Searched in: {os.path.abspath(process_dir)}")
            sys.exit(1)

    executor = ProcessExecutor(process_file)
    executor.execute()


@cli.command()
def ls():
    """List available process definitions."""
    process_dir = os.environ.get('PROCEDURE_DIR', './procedures')
    
    if not os.path.isdir(process_dir):
        click.echo(f"Procedure directory not found: {process_dir}")
        click.echo("Set the PROCEDURE_DIR environment variable to specify the location.")
        return
    
    processes = []
    for filename in os.listdir(process_dir):
        if filename.endswith('.yml') or filename.endswith('.yaml'):
            try:
                with open(os.path.join(process_dir, filename), 'r') as f:
                    process = yaml.safe_load(f)
                    processes.append({
                        'name': process.get('name', 'Unnamed process'),
                        'description': process.get('description', 'No description'),
                        'filename': filename[:-4]
                    })
            except Exception as e:
                click.echo(f"Error reading {filename}: {e}", err=True)
    
    if not processes:
        click.echo("No process definitions found.")
        return
    
    click.echo(f"Found {len(processes)} procedure(s):")
    for idx, process in enumerate(processes, 1):
        click.echo(f"\n{idx}. {process['name']} (ID: {process['filename']})")
        click.echo(f"   {process['description']}")

# Standalone command entry points for setuptools
def run_command():
    """Entry point for the run command."""
    sys.argv[0] = 'process-run'
    sys.exit(run(standalone_mode=False))

def ls_command():
    """Entry point for the list command."""
    sys.argv[0] = 'process-ls'
    sys.exit(ls(standalone_mode=False))

if __name__ == '__main__':
    cli()