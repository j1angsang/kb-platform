-- psql -U postgres -d file_storage_db -v ro_password='...' -f scripts/create_text2sql_readonly.sql
CREATE ROLE kb_readonly LOGIN PASSWORD :'ro_password';
GRANT CONNECT ON DATABASE file_storage_db TO kb_readonly;
GRANT USAGE ON SCHEMA analytics TO kb_readonly;
GRANT SELECT ON analytics.v_employee_directory TO kb_readonly;
ALTER ROLE kb_readonly SET default_transaction_read_only = on;
