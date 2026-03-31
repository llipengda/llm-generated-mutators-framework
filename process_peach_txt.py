with open('peach/peach.txt', 'r') as f:
    lines = f.readlines()

keep_sections = [
    'Analyzer',
    'Data Element',
    'Relation',
    'Transformer'
]

with open('peach/peach.txt', 'w') as f:
    keep = True
    for line in lines:
        if line.startswith('----'):
            if any(section in line for section in keep_sections):
                keep = True
            else:
                keep = False
        if keep and line.strip():
            f.write(line)