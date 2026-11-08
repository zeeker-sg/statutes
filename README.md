# Statutes Database Project

A Zeeker project for managing the statutes database.

## Getting Started

1. Add dependencies for your data sources:
   ```bash
   uv add requests beautifulsoup4  # Example: web scraping dependencies
   ```

2. Add resources:
   ```bash
   uv run zeeker add my_resource --description "Description of the resource"
   ```

3. Create `.env` file for credentials (optional):
   ```bash
   # Create .env file with API keys and S3 credentials
   echo "MY_API_KEY=your_api_key_here" > .env
   ```

4. Implement data fetching in `resources/my_resource.py`

5. Build the database:
   ```bash
   uv run zeeker build
   ```

6. Deploy to S3:
   ```bash
   uv run zeeker deploy
   ```

## Automated Deployment

This project includes a GitHub Action that automatically builds and deploys to S3:

- **Triggers:** Pushes to main/master branch, or manual dispatch
- **Required Secrets:** Configure in GitHub repository settings:
  - `S3_BUCKET` - Target S3 bucket name
  - `AWS_ACCESS_KEY_ID` - AWS access key
  - `AWS_SECRET_ACCESS_KEY` - AWS secret key
  - `JINA_API_TOKEN` - (optional) For Jina Reader resources
  - `OPENAI_API_KEY` - (optional) For OpenAI resources
- **Workflow:** `.github/workflows/deploy.yml`

To deploy manually: Go to Actions tab → "Deploy Zeeker Project to S3" → Run workflow

## Project Structure

- `pyproject.toml` - Project dependencies and metadata
- `zeeker.toml` - Project configuration
- `resources/` - Python modules for data fetching
- `.env` - Environment variables (gitignored, create manually)
- `statutes.db` - Generated SQLite database (gitignored)
- `.venv/` - Virtual environment (gitignored)

## Dependencies

This project uses uv for dependency management. Common dependencies for data projects:

- `requests` - HTTP API calls
- `beautifulsoup4` - Web scraping and HTML parsing
- `pandas` - Data processing and analysis
- `lxml` - XML parsing
- `pdfplumber` - PDF text extraction
- `openpyxl` - Excel file reading

Add dependencies with: `uv add package_name`

## Environment Variables

Zeeker automatically loads `.env` files during build and deployment:

```bash
# S3 deployment (required for deploy)
S3_BUCKET=your-bucket-name
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key

# API keys for your resources
JINA_API_TOKEN=your-jina-token
OPENAI_API_KEY=your-openai-key
```

## Development

Format and lint code:
- `uv run black .` - Format code with black
- `uv run ruff check .` - Lint code with ruff
- `uv run ruff check --fix .` - Auto-fix ruff issues

## Resources

