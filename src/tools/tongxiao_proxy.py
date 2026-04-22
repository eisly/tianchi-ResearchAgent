import sys
import os
import subprocess
import threading
import shutil
import time

# Use temp directory for logs
LOG_FILE = os.path.join(os.environ.get("TEMP", "."), "tongxiao_proxy.log")

def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}\n")
    except:
        pass

def forward_stdin(process):
    try:
        while True:
            # Read from sys.stdin.buffer (binary)
            data = sys.stdin.buffer.read(1024)
            if not data:
                break
            process.stdin.write(data)
            process.stdin.flush()
    except Exception as e:
        log(f"Stdin error: {e}")
    finally:
        try:
            process.stdin.close()
        except:
            pass

def forward_stderr(process):
    try:
        while True:
            # Read from process.stderr (binary)
            data = process.stderr.read(1024)
            if not data:
                break
            sys.stderr.buffer.write(data)
            sys.stderr.buffer.flush()
            # Log stderr for debugging
            try:
                log(f"Stderr: {data.decode('utf-8', errors='ignore').strip()}")
            except:
                pass
    except Exception as e:
        log(f"Stderr loop error: {e}")

def main():
    # Clear log file on start
    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - Proxy started\n")
    except:
        pass

    npx_cmd = "npx.cmd" if os.name == 'nt' else "npx"
    npx_path = shutil.which(npx_cmd)
    
    if not npx_path:
        log(f"Warning: {npx_cmd} not found in PATH. Using command name directly.")
        npx_path = npx_cmd

    cmd = [npx_path, "--quiet", "-y", "@tongxiao/common-search-mcp-server"]
    
    # Pass arguments from command line if any
    cmd.extend(sys.argv[1:])
    
    log(f"Running command: {cmd}")
    
    # Debug: log environment keys to see if we have APPDATA etc.
    log(f"Environment keys count: {len(os.environ)}")
    if 'APPDATA' in os.environ:
        log(f"APPDATA: {os.environ['APPDATA']}")
    else:
        log("APPDATA not set!")

    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ,
            bufsize=0  # Unbuffered
        )
    except Exception as e:
        log(f"Failed to start process: {e}")
        sys.stderr.write(f"Failed to start process: {e}\n")
        sys.exit(1)

    log(f"Process started with PID {process.pid}")

    # Thread for stdin
    t_in = threading.Thread(target=forward_stdin, args=(process,))
    t_in.daemon = True
    t_in.start()

    # Thread for stderr
    t_err = threading.Thread(target=forward_stderr, args=(process,))
    t_err.daemon = True
    t_err.start()

    # Main thread filters stdout
    try:
        while True:
            line = process.stdout.readline()
            if not line:
                break
            
            # Check for the problematic line
            if b"TongXiao MCP Server running on stdio" in line:
                log(f"Filtered startup message: {line}")
                
                # Logic: remove the message string. 
                # If line was just that message (with optional newline), it becomes empty or just newline.
                # If just newline, we can skip it or write it. 
                # Better to skip to avoid empty lines if not needed, but empty lines are valid in JSON-RPC (ignored).
                
                cleaned = line.replace(b"TongXiao MCP Server running on stdio", b"")
                # Remove leading/trailing whitespace to see if anything meaningful remains
                if not cleaned.strip():
                    continue
                
                line = cleaned

            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            
    except Exception as e:
        log(f"Stdout loop error: {e}")
    finally:
        log("Terminating process")
        process.terminate()
        process.wait()
        log(f"Process exited with code {process.returncode}")
        sys.exit(process.returncode)

if __name__ == "__main__":
    main()
