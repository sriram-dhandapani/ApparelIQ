-- Step 9: least-privilege database user for the coworker agent's user-assigned managed identity (SYNTHETIC demo).
-- Placeholders are substituted by databricks/notebooks/03_apply_sql_script:
--   {{AGENT_PRINCIPAL_NAME}}  display name of the managed identity (id-appareliq-agent)
--   {{AGENT_CLIENT_ID}}       its client (application) id
-- SID + TYPE = E maps the user to the client id directly (no Directory Readers needed on the server identity).

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'{{AGENT_PRINCIPAL_NAME}}')
BEGIN
    DECLARE @sid nvarchar(64) = CONVERT(nvarchar(64), CONVERT(varbinary(16), CAST('{{AGENT_CLIENT_ID}}' AS uniqueidentifier)), 1);
    EXEC(N'CREATE USER [{{AGENT_PRINCIPAL_NAME}}] WITH SID = ' + @sid + N', TYPE = E;');
END
GO

-- Read evidence (lanes, prices, guardrails, DC positions, owners) and the agent's own case store.
GRANT SELECT ON SCHEMA::[ref] TO [{{AGENT_PRINCIPAL_NAME}}];
GRANT SELECT ON SCHEMA::[erp] TO [{{AGENT_PRINCIPAL_NAME}}];
GRANT SELECT ON SCHEMA::[wms] TO [{{AGENT_PRINCIPAL_NAME}}];
GRANT SELECT, EXECUTE ON SCHEMA::[agent] TO [{{AGENT_PRINCIPAL_NAME}}];
GO

-- No direct writes anywhere: every change goes through agent.usp_* (ownership chaining lets the procedures write).
DENY INSERT, UPDATE, DELETE, ALTER ON SCHEMA::[ref] TO [{{AGENT_PRINCIPAL_NAME}}];
DENY INSERT, UPDATE, DELETE, ALTER ON SCHEMA::[erp] TO [{{AGENT_PRINCIPAL_NAME}}];
DENY INSERT, UPDATE, DELETE, ALTER ON SCHEMA::[wms] TO [{{AGENT_PRINCIPAL_NAME}}];
DENY INSERT, UPDATE, DELETE, ALTER ON SCHEMA::[agent] TO [{{AGENT_PRINCIPAL_NAME}}];
GO
