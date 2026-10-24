# Synthetic WHT Data for VS Code

This folder contains synthetic CSV files derived from the structure of the supplied workbooks. It is designed for prototyping classification, joins, WHT calculations, and Copilot-assisted coding without using the original business records.

## Files
- `open_line_items_synthetic.csv`: transaction/line-level test data
- `material_data_synthetic.csv`: purchasing-document item details
- `wht_mapping_synthetic.csv`: supplier, cost category and WHT-rate rules
- `countries_synthetic.csv`: synthetic supplier-country mapping
- `cost_types_synthetic.csv`: authoritative Cost Type reference catalog
- `cost_type_decisions_synthetic.csv`: labeled cost-type examples
- `example_load.py`: minimal VS Code loading and join example
- `metadata.json`: row counts and generation notes

## Run
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install pandas
python example_load.py
```

## Important
This dataset is synthetic and suitable for development/testing. It must not be treated as tax advice, a production decision table, or evidence of an actual posting. Validate any model or rule engine against approved tax logic before production use.
