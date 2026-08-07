# Numista Collection — automated GitHub Pages

The public repository stores the generator, public catalogue caches and **sanitized** collection inputs. GitHub Actions generates the HTML and `summary.json`; generated files are not committed.

## Normal update

Use the replacement local `out\publish.bat`. It converts the full Numista exports into minimal public CSV files and pushes only:

- N# number
- quantity
- year
- title
- issuer
- grade

Buying prices, estimates, private comments, storage locations, acquisition details, serial numbers and internal IDs are not uploaded.

GitHub Actions then runs the Python generator and deploys the page.

## Required repository secret

Create an Actions secret named `NUMISTA_API_KEY`. It is used only when a type or issuer is not already available in the public cache. The API key is never committed.
