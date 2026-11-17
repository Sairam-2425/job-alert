"""Skill dictionary for no-AI (local) mode. Format per line: Canonical|alias|alias ...
Add your own lines, or list extra skills in config.json -> preferences.extra_skills."""
from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from typing import Dict, List, Pattern, Tuple

CATALOG = """
Python|python3
Java
JavaScript|js|ecmascript
TypeScript
C++|cpp
C#|csharp
Golang|go lang|go programming
Rust
Ruby|ruby on rails|rails
PHP|laravel
Swift
Kotlin
Scala
R Programming|r language|rstudio|ggplot
MATLAB
Perl
Shell Scripting|bash|shell script|zsh
PowerShell
SQL|t-sql|pl/sql|plsql
NoSQL
PostgreSQL|postgres
MySQL
SQL Server|mssql
Oracle
MongoDB
Redis
Elasticsearch|opensearch
Cassandra
DynamoDB
Snowflake
BigQuery
Redshift
Databricks
Apache Spark|spark|pyspark
Hadoop|hdfs|hive
Kafka|apache kafka
Airflow|apache airflow
dbt
ETL|elt|data pipelines|data pipeline
Data Warehousing|data warehouse
Data Modeling
Pandas
NumPy
SciPy
Scikit-learn|sklearn
TensorFlow
PyTorch
Keras
XGBoost
Machine Learning|ml
Deep Learning
NLP|natural language processing
Computer Vision|opencv
Generative AI|genai|gen ai
LLM|llms|large language models
LangChain
Hugging Face|huggingface|transformers
MLOps
Data Analysis|data analytics
Data Science
Data Engineering
Statistics
A/B Testing
Tableau
Power BI|powerbi
Looker
Excel|ms excel|advanced excel
AWS|amazon web services|ec2|s3|aws lambda|cloudformation
Azure|microsoft azure
GCP|google cloud
Docker
Kubernetes|k8s
Terraform
Ansible
Jenkins
GitHub Actions
GitLab CI
CI/CD|cicd|continuous integration
DevOps
Git|github|gitlab|bitbucket
Linux|unix
Nginx
Prometheus
Grafana
Datadog
Splunk
Networking|tcp/ip
Cybersecurity|information security|infosec
React|react.js|reactjs
Angular|angularjs
Vue|vue.js|vuejs
Next.js|nextjs
Node.js|nodejs|node
Express|express.js
Django
Flask
FastAPI
Spring Boot|spring
.NET|dotnet|asp.net
REST API|restful|rest apis|rest
GraphQL
gRPC
Microservices
HTML|html5
CSS|css3
Tailwind CSS|tailwind
Sass|scss
Redux
Webpack
jQuery
Android
iOS
Flutter
React Native
Selenium
Cypress
Playwright
Pytest
JUnit
Postman
Test Automation|automation testing
Manual Testing|qa
JIRA
Agile
Scrum
Kanban
Figma
UI/UX|ux design|ui design
SAP
Salesforce
ServiceNow
Workday
RabbitMQ
Celery
OAuth|jwt
System Design
Object-Oriented Programming|oop
Data Structures|algorithms
Embedded Systems|embedded c
IoT
Blockchain|solidity
Unity
Power Automate
Google Analytics
SEO
Project Management
Stakeholder Management
Technical Writing
"""


@lru_cache(maxsize=1)
def _compiled() -> List[Tuple[str, Pattern]]:
    entries = []
    for line in CATALOG.strip().splitlines():
        names = [n.strip() for n in line.split("|") if n.strip()]
        canonical, aliases = names[0], names
        pattern = "|".join(re.escape(a.lower()) for a in sorted(set(aliases), key=len, reverse=True))
        entries.append((canonical, re.compile(r"(?<![a-z0-9+#.])(?:" + pattern + r")(?![a-z0-9+#])", re.I)))
    return entries


def find_skills(text: str, extra: List[str] = ()) -> Dict[str, int]:
    """Return {canonical skill: mention count}, ordered by count desc then first appearance."""
    counts: Counter = Counter()
    first: Dict[str, int] = {}
    for name, rx in _compiled():
        hits = list(rx.finditer(text))
        if hits:
            counts[name] = len(hits)
            first[name] = hits[0].start()
    for name in extra:
        rx = re.compile(r"(?<![a-z0-9+#.])" + re.escape(name.lower()) + r"(?![a-z0-9+#])", re.I)
        hits = list(rx.finditer(text))
        if hits and name not in counts:
            counts[name], first[name] = len(hits), hits[0].start()
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], first[kv[0]])))
