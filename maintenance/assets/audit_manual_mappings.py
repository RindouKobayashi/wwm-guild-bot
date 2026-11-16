"""Inventory public runtime constant containers without exporting their values."""
import ast
import json
from pathlib import Path

BOT=Path(__file__).resolve().parents[2]


def inventory():
    files=sorted(set((BOT/'cogs').glob('*.py'))|set((BOT/'utility').glob('*.py'))|set((BOT/'activity').glob('*.py')))
    definitions=[];uses={}
    for path in files:
        tree=ast.parse(path.read_text(encoding='utf-8'));relative=path.relative_to(BOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node,ast.Name) and isinstance(node.ctx,ast.Load):uses.setdefault(node.id,set()).add(relative)
            if not isinstance(node,ast.Assign) or not isinstance(node.value,(ast.Dict,ast.List,ast.Set,ast.Tuple)):continue
            for target in node.targets:
                if not isinstance(target,ast.Name) or not target.id.isupper() or target.id.startswith('_'):continue
                try:value=ast.literal_eval(node.value)
                except (ValueError,TypeError):continue
                definitions.append(dict(symbol=target.id,file=relative,line=node.lineno,kind=type(value).__name__,entries=len(value)))
    for row in definitions:row['consumers']=sorted(uses.get(row['symbol'],set()))
    return dict(format=1,coverage='Literal uppercase containers in runtime cogs, utility and Activity Python. Values excluded. Generated bundles and dynamic/DB mappings audited separately in the companion report.',mappings=definitions)


if __name__=='__main__':
    output=BOT/'maintenance/assets/martial-mapping-inventory.json'
    output.write_text(json.dumps(inventory(),indent=2)+'\n',encoding='utf-8');print(output)
