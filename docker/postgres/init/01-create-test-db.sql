SELECT 'CREATE DATABASE csv_pipeline_test'
WHERE NOT EXISTS (
    SELECT FROM pg_database WHERE datname = 'csv_pipeline_test'
)
\gexec
