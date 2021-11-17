from copy import deepcopy
import re
from pathlib import Path

import nbformat
import jupytext
from jinja2 import Template

from soorgeon import exceptions, static_analysis

_PICKLING_TEMPLATE = Template("""
{% for product in products %}
Path(product['{{product}}']).write_bytes(pickle.dumps({{product}}))
{% endfor %}
""")

_UNPICKLING_TEMPLATE = Template("""
{% for up, key in up_and_in %}
{{key}} = pickle.loads(Path(upstream['{{up}}']['{{key}}']).read_bytes())
{% endfor %}
""")


class ProtoTask:
    """A group of cells that will be converted into a Ploomber task
    """
    def __init__(self, name, cells):
        self._name = name
        self._cells = cells

    @property
    def name(self):
        return self._name

    def exposes(self):
        """Return a list of variables that this prototask creates
        """
        pass

    def uses(self):
        """Return a list of variables that this prototask uses
        """
        pass

    def _pickling_cell(self, io):
        """Add cell that pickles the outputs
        """
        _, outputs = io[self.name]

        if outputs:
            pickling = nbformat.v4.new_code_cell(
                source=_PICKLING_TEMPLATE.render(products=outputs))
            pickling.metadata['tags'] = ['soorgeon-pickle']

            return pickling
        else:
            return None

    def _unpickling_cell(self, io, providers):
        """Add cell that unpickles the inputs
        """
        inputs, _ = io[self.name]

        if inputs:
            up_and_in = [(providers.get(input_, self.name), input_)
                         for input_ in inputs]

            unpickling = nbformat.v4.new_code_cell(
                source=_UNPICKLING_TEMPLATE.render(up_and_in=up_and_in))
            unpickling.metadata['tags'] = ['soorgeon-unpickle']

            return unpickling
        else:
            return None

    def _add_parameters_cell(self, cells, upstream):
        """Add parameters cell at the top
        """
        source = ''

        upstream_current = upstream[self.name]

        if upstream_current:
            source += f'upstream = {list(upstream_current)}\n'
        else:
            source += 'upstream = None\n'

        source += 'product = None'

        parameters = nbformat.v4.new_code_cell(source=source)
        parameters.metadata['tags'] = ['parameters']

        return [parameters] + cells

    def _add_imports_cell(self, code_nb, add_pathlib_and_pickle):
        # FIXME: instatiate this in the constructor so we only build it once
        ip = static_analysis.ImportsParser(code_nb)
        source = ip.get_imports_cell_for_task(
            static_analysis.remove_imports(str(self)))

        # FIXME: only add them if they're not already there
        if add_pathlib_and_pickle:
            source = source or ''
            source += '\nfrom pathlib import Path'
            source += '\nimport pickle'

        if source:
            cell = nbformat.v4.new_code_cell(source=source)
            cell.metadata['tags'] = ['soorgeon-imports']
            return cell

    def export(self, upstream, io, providers, code_nb):
        """Export as a Python string
        """

        nb = nbformat.v4.new_notebook()
        # TODO: simplify, make each function return a single cell and then join
        # here

        cells = deepcopy(self._cells)

        # remove import statements from code cells
        for cell in cells:
            if cell.cell_type == 'code':
                cell['source'] = static_analysis.remove_imports(cell['source'])

        # remove empty cells and whitespace-only cells (we may have some after
        # removing imports)
        cells = [cell for cell in cells if cell['source'].strip()]

        cell_unpickling = self._unpickling_cell(io, providers)

        if cell_unpickling:
            cells = [cell_unpickling] + cells

        cells = self._add_parameters_cell(cells, upstream)

        cell_pickling = self._pickling_cell(io)

        if cell_pickling:
            cells = cells + [cell_pickling]

        cell_imports = self._add_imports_cell(
            code_nb, add_pathlib_and_pickle=cell_pickling or cell_unpickling)

        pre = [cell_imports] if cell_imports else []

        nb.cells = pre + cells

        # TODO: H2 header should be the top cell

        return jupytext.writes(nb, fmt='py:percent')

    def to_spec(self, io):
        _, outputs = io[self.name]

        # prefix products by name to guarantee they're unique
        products = {
            out: str(Path('output', f'{self.name}-{out}.pkl'))
            for out in outputs
        }

        # FIXME: check that there isn't an nb key already
        products['nb'] = str(Path('output', f'{self.name}.ipynb'))

        return {
            'source': str(Path('tasks', self.name + '.py')),
            'product': products
        }

    def __str__(self):
        """Retun the task as string (only code cells)
        """
        return '\n'.join(cell['source'] for cell in self._cells
                         if cell.cell_type == 'code')


class ProtoDAGSpec:
    """A group of prototasks that will be converted into a Ploomber Spec
    """
    def __init__(self, proto_tasks):
        pass

    def clean_imports(self):
        """Clean import statements
        """
        pass


def find_breaks(nb):
    """Find index breaks based on H2 markdown indexes

    Notes
    -----
    The first element of the returned list may be >0 if the first H2 header
    isn't in the first cell
    """
    breaks = []

    # TODO: this should return named tuples with index and extracted names
    for idx, cell in enumerate(nb.cells):
        # TODO: more robust H2 detector
        if cell.cell_type == 'markdown' and _get_h2_header(cell.source):
            breaks.append(idx)

    if not breaks:
        raise exceptions.InputError('Expected to have at least one markdown '
                                    'with a level 2 header')

    return breaks


def split_with_breaks(cells, breaks):
    """Split a list of cells at given indexes

    Notes
    -----
    Given that the first index has the cell indx of the first H2 header, but
    there may be code in ealier cells, we ignore it. The first split is always
    from 0 to breaks[1]
    """
    breaks = breaks + [None]
    breaks[0] = 0

    cells_split = []

    for left, right in zip(breaks, breaks[1:]):
        cells_split.append(cells[left:right])

    return cells_split


def names_with_breaks(cells, breaks):
    return [_get_h2_header(cells[break_]['source']) for break_ in breaks]


def _sanitize_name(name):
    return name.lower().replace(' ', '-')


def _get_h2_header(md):
    lines = md.splitlines()

    found = None

    for line in lines:
        match = re.search(r'\s*##\s+(.+)', line)

        if match:
            found = _sanitize_name(match.group(1))

            break

    return found
