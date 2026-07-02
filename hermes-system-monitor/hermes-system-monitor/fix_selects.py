import re

with open("database.py", "r") as f:
    content = f.read()

# Fix all SELECT statements that end with systemd_failed_count
# Pattern: "systemd_failed_count\n" followed by FROM or closing paren

# 1. Fix SELECT in queries (add sub2api columns)
lines = content.split('\n')
new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    # Check if this line ends with systemd_failed_count and next line has FROM or is a closing
    if 'systemd_failed_count' in line and 'sub2api_rx_kbps' not in line:
        # Check if this is a SELECT column list (not CREATE TABLE or INSERT)
        if 'REAL' not in line and 'VALUES' not in line and 'for column in' not in line:
            # Add sub2api columns
            if line.rstrip().endswith(','):
                # Already has comma, just add
                new_lines.append(line)
                if i+1 < len(lines) and 'sub2api_rx_kbps' not in lines[i+1]:
                    indent = ' ' * 12
                    new_lines.append(f'{indent}sub2api_rx_kbps, sub2api_tx_kbps,')
            else:
                # Add comma and new columns
                new_lines.append(line.rstrip() + ',')
                if i+1 < len(lines) and 'sub2api_rx_kbps' not in lines[i+1]:
                    indent = ' ' * 12
                    new_lines.append(f'{indent}sub2api_rx_kbps, sub2api_tx_kbps')
            i += 1
            continue
    new_lines.append(line)
    i += 1

content = '\n'.join(new_lines)

with open("database.py", "w") as f:
    f.write(content)

print("✅ Fixed all SELECT statements")
