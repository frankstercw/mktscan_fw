from setuptools import setup, find_packages

setup(
    name="mktscan",
    version="1.0.0",
    description="Market Intelligence Scraper & Sentiment Engine",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "yfinance>=0.2.36",
        "requests>=2.31.0",
        "beautifulsoup4>=4.12.0",
        "lxml>=5.0.0",
        "pandas>=2.1.0",
        "numpy>=1.26.0",
        "pydantic>=2.5.0",
        "transformers>=4.36.0",
        "torch>=2.1.0",
        "vaderSentiment>=3.3.2",
        "apscheduler>=3.10.0",
        "sqlalchemy>=2.0.0",
        "streamlit>=1.29.0",
        "plotly>=5.18.0",
        "pyyaml>=6.0.1",
        "python-dotenv>=1.0.0",
        "click>=8.1.7",
        "rich>=13.7.0",
        "tenacity>=8.2.3",
        "openai>=1.6.0",
    ],
    entry_points={
        "console_scripts": [
            "mktscan=mktscan.cli:main",
        ],
    },
)
