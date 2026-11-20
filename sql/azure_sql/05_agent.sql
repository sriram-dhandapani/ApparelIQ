-- Step 9: coworker agent case store, Tier 1 staging procedures and the Tier 2 action gateway (SYNTHETIC demo).
-- docs/requirements.md §8 (AA-01..AA-05, AC-01, gateway contract), §9 (HL-01..HL-11, routing matrix).
--
-- The agent identity gets EXECUTE on schema [agent] and SELECT on ref/erp/wms only (06_agent_grants.sql).
-- Writes to erp.allocation_decision, erp.stock_transfer_order and wms.inventory_position happen ONLY inside
-- agent.usp_record_decision, after a named human approves in Teams. Ownership chaining (all schemas owned by
-- dbo, no dynamic SQL) lets the procedures write although the caller has no direct INSERT/UPDATE.
-- Idempotent: safe to re-run (CREATE ... IF NOT EXISTS, CREATE OR ALTER).

IF SCHEMA_ID(N'agent') IS NULL EXEC(N'CREATE SCHEMA [agent] AUTHORIZATION [dbo]');
GO

-- Persona -> Entra user map (loaded by 06_agent_personas.sql from the git-ignored deployment config).
IF OBJECT_ID(N'agent.persona_user') IS NULL
CREATE TABLE [agent].[persona_user] (
    [person_id]      NVARCHAR(16)     NOT NULL,
    [upn]            NVARCHAR(256)    NOT NULL,
    [aad_object_id]  UNIQUEIDENTIFIER NOT NULL,
    [display_name]   NVARCHAR(80)     NULL,
    [is_escalation]  BIT              NOT NULL CONSTRAINT [df_agent_persona_esc] DEFAULT 0,
    [record_label]   NVARCHAR(16)     NOT NULL CONSTRAINT [df_agent_persona_label] DEFAULT N'SYNTHETIC',
    CONSTRAINT [pk_agent_persona_user] PRIMARY KEY ([person_id])
);
GO

-- AA-01: one case per style x market while it is active.
IF OBJECT_ID(N'agent.case') IS NULL
CREATE TABLE [agent].[case] (
    [case_id]                    NVARCHAR(40)     NOT NULL,
    [trigger_id]                 NVARCHAR(8)      NOT NULL,
    [style_id]                   NVARCHAR(16)     NOT NULL,
    [market_id]                  NVARCHAR(16)     NOT NULL,
    [category]                   NVARCHAR(16)     NULL,
    [lifecycle]                  NVARCHAR(16)     NULL,
    [severity]                   NVARCHAR(16)     NOT NULL,
    [summary]                    NVARCHAR(1000)   NULL,
    [case_status]                NVARCHAR(24)     NOT NULL,
    [chat_id]                    NVARCHAR(256)    NULL,
    [card_activity_id]           NVARCHAR(256)    NULL,
    [evidence_json]              NVARCHAR(MAX)    NULL,
    [recommendation_json]        NVARCHAR(MAX)    NULL,
    [recommended_option_id]      NVARCHAR(8)      NULL,
    [pending_decision]           NVARCHAR(16)     NULL,
    [pending_option_id]          NVARCHAR(8)      NULL,
    [pending_units]              INT              NULL,
    [pending_approver_upn]       NVARCHAR(256)    NULL,
    [pending_approver_person_id] NVARCHAR(16)     NULL,
    [pending_rationale]          NVARCHAR(2000)   NULL,
    [pending_reason_code]        NVARCHAR(32)     NULL,
    [defer_until_utc]            DATETIME2(0)     NULL,
    [decision_id]                UNIQUEIDENTIFIER NULL,
    [opened_at_utc]              DATETIME2(0)     NOT NULL CONSTRAINT [df_agent_case_opened] DEFAULT SYSUTCDATETIME(),
    [updated_at_utc]             DATETIME2(0)     NOT NULL CONSTRAINT [df_agent_case_updated] DEFAULT SYSUTCDATETIME(),
    [record_label]               NVARCHAR(16)     NOT NULL CONSTRAINT [df_agent_case_label] DEFAULT N'SYNTHETIC',
    CONSTRAINT [pk_agent_case] PRIMARY KEY ([case_id]),
    CONSTRAINT [ck_agent_case_status] CHECK ([case_status] IN
        (N'OPEN', N'AWAITING_APPROVAL', N'NEEDS_FINANCE', N'DEFERRED', N'APPROVED', N'REJECTED', N'CLOSED')),
    CONSTRAINT [ck_agent_case_synthetic] CHECK ([record_label] = N'SYNTHETIC')
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ux_agent_case_active')
CREATE UNIQUE INDEX [ux_agent_case_active] ON [agent].[case] ([style_id], [market_id])
    WHERE [case_status] IN (N'OPEN', N'AWAITING_APPROVAL', N'NEEDS_FINANCE', N'DEFERRED');
GO

-- AA-02: who the agent pulled into the case chat and why.
IF OBJECT_ID(N'agent.case_member') IS NULL
CREATE TABLE [agent].[case_member] (
    [case_id]        NVARCHAR(40)  NOT NULL,
    [person_id]      NVARCHAR(16)  NOT NULL,
    [upn]            NVARCHAR(256) NOT NULL,
    [case_role]      NVARCHAR(32)  NOT NULL,
    [added_at_utc]   DATETIME2(0)  NOT NULL CONSTRAINT [df_agent_member_added] DEFAULT SYSUTCDATETIME(),
    [record_label]   NVARCHAR(16)  NOT NULL CONSTRAINT [df_agent_member_label] DEFAULT N'SYNTHETIC',
    CONSTRAINT [pk_agent_case_member] PRIMARY KEY ([case_id], [person_id], [case_role]),
    CONSTRAINT [fk_agent_case_member_case] FOREIGN KEY ([case_id]) REFERENCES [agent].[case]([case_id])
);
GO

-- AA-04: draft transfer lines per option. PROPOSED drafts reserve nothing and cannot ship.
IF OBJECT_ID(N'agent.transfer_draft') IS NULL
CREATE TABLE [agent].[transfer_draft] (
    [case_id]            NVARCHAR(40)  NOT NULL,
    [option_id]          NVARCHAR(8)   NOT NULL,
    [line_no]            INT           NOT NULL,
    [sku_id]             NVARCHAR(32)  NOT NULL,
    [from_location_id]   NVARCHAR(16)  NOT NULL,
    [to_location_id]     NVARCHAR(16)  NOT NULL,
    [units]              INT           NOT NULL CONSTRAINT [ck_agent_draft_units] CHECK ([units] > 0),
    [released_units]     INT           NULL,
    [lane_mode]          NVARCHAR(16)  NULL,
    [lead_days]          INT           NULL,
    [cost_per_unit_usd]  DECIMAL(19,4) NOT NULL,
    [full_price_usd]     DECIMAL(19,4) NOT NULL,
    [unit_cost_usd]      DECIMAL(19,4) NOT NULL,
    [draft_status]       NVARCHAR(16)  NOT NULL CONSTRAINT [df_agent_draft_status] DEFAULT N'PROPOSED',
    [created_at_utc]     DATETIME2(0)  NOT NULL CONSTRAINT [df_agent_draft_created] DEFAULT SYSUTCDATETIME(),
    [record_label]       NVARCHAR(16)  NOT NULL CONSTRAINT [df_agent_draft_label] DEFAULT N'SYNTHETIC',
    CONSTRAINT [pk_agent_transfer_draft] PRIMARY KEY ([case_id], [option_id], [line_no]),
    CONSTRAINT [ck_agent_draft_status] CHECK ([draft_status] IN (N'PROPOSED', N'RELEASED', N'CANCELLED')),
    CONSTRAINT [fk_agent_draft_case] FOREIGN KEY ([case_id]) REFERENCES [agent].[case]([case_id])
);
GO

-- Every agent and human action, by tier. action_id is the idempotency key (gateway contract #4).
IF OBJECT_ID(N'agent.action_log') IS NULL
CREATE TABLE [agent].[action_log] (
    [action_id]       NVARCHAR(80)   NOT NULL,
    [case_id]         NVARCHAR(40)   NULL,
    [tier]            TINYINT        NOT NULL,
    [action_type]     NVARCHAR(32)   NOT NULL,
    [actor]           NVARCHAR(256)  NOT NULL,
    [outcome]         NVARCHAR(24)   NOT NULL,
    [detail_json]     NVARCHAR(MAX)  NULL,
    [created_at_utc]  DATETIME2(0)   NOT NULL CONSTRAINT [df_agent_log_created] DEFAULT SYSUTCDATETIME(),
    [record_label]    NVARCHAR(16)   NOT NULL CONSTRAINT [df_agent_log_label] DEFAULT N'SYNTHETIC',
    CONSTRAINT [pk_agent_action_log] PRIMARY KEY ([action_id])
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_agent_action_log_case')
CREATE INDEX [ix_agent_action_log_case] ON [agent].[action_log] ([case_id], [created_at_utc]);
GO

-- AA-05: follow-up checks (T+7 / T+14 after execution, re-open when a deferral expires).
IF OBJECT_ID(N'agent.follow_up') IS NULL
CREATE TABLE [agent].[follow_up] (
    [follow_up_id]   INT IDENTITY(1,1) NOT NULL,
    [case_id]        NVARCHAR(40)  NOT NULL,
    [task_type]      NVARCHAR(24)  NOT NULL,
    [due_at_utc]     DATETIME2(0)  NOT NULL,
    [task_status]    NVARCHAR(16)  NOT NULL CONSTRAINT [df_agent_fu_status] DEFAULT N'PENDING',
    [completed_at_utc] DATETIME2(0) NULL,
    [record_label]   NVARCHAR(16)  NOT NULL CONSTRAINT [df_agent_fu_label] DEFAULT N'SYNTHETIC',
    CONSTRAINT [pk_agent_follow_up] PRIMARY KEY ([follow_up_id]),
    CONSTRAINT [ck_agent_fu_status] CHECK ([task_status] IN (N'PENDING', N'DONE', N'CANCELLED')),
    CONSTRAINT [fk_agent_fu_case] FOREIGN KEY ([case_id]) REFERENCES [agent].[case]([case_id])
);
GO

------------------------------------------------------------------------------------------------------------------
-- Tier 1 procedures (agent runs them autonomously; logged and reversible)
------------------------------------------------------------------------------------------------------------------

CREATE OR ALTER PROCEDURE [agent].[usp_log_action]
    @action_id NVARCHAR(80), @case_id NVARCHAR(40), @tier TINYINT, @action_type NVARCHAR(32),
    @actor NVARCHAR(256), @outcome NVARCHAR(24), @detail_json NVARCHAR(MAX) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    IF @detail_json IS NOT NULL AND ISJSON(@detail_json) = 0 THROW 50001, N'detail_json is not valid JSON', 1;
    DECLARE @inserted INT = 0;
    IF NOT EXISTS (SELECT 1 FROM [agent].[action_log] WHERE [action_id] = @action_id)
    BEGIN
        INSERT INTO [agent].[action_log] ([action_id], [case_id], [tier], [action_type], [actor], [outcome], [detail_json])
        VALUES (@action_id, @case_id, @tier, @action_type, @actor, @outcome, @detail_json);
        SET @inserted = @@ROWCOUNT;
    END
    SELECT @inserted AS inserted;
END
GO

-- AA-01: open (or reuse) the active case for a style x market. Returns one row: case_id, is_new, reopened, case_status, chat_id.
CREATE OR ALTER PROCEDURE [agent].[usp_open_case]
    @trigger_id NVARCHAR(8), @style_id NVARCHAR(16), @market_id NVARCHAR(16), @severity NVARCHAR(16),
    @summary NVARCHAR(1000), @evidence_json NVARCHAR(MAX), @recommendation_json NVARCHAR(MAX),
    @recommended_option_id NVARCHAR(8), @actor NVARCHAR(256) = N'agent'
AS
BEGIN
    SET NOCOUNT ON; SET XACT_ABORT ON;
    IF ISJSON(@evidence_json) = 0 OR ISJSON(@recommendation_json) = 0 THROW 50002, N'evidence/recommendation must be JSON', 1;
    DECLARE @case_id NVARCHAR(40), @status NVARCHAR(24), @chat NVARCHAR(256), @defer DATETIME2(0), @is_new BIT = 0, @reopened BIT = 0;
    BEGIN TRANSACTION;
    SELECT @case_id = [case_id], @status = [case_status], @chat = [chat_id], @defer = [defer_until_utc]
    FROM [agent].[case] WITH (UPDLOCK, HOLDLOCK)
    WHERE [style_id] = @style_id AND [market_id] = @market_id
      AND [case_status] IN (N'OPEN', N'AWAITING_APPROVAL', N'NEEDS_FINANCE', N'DEFERRED');

    IF @case_id IS NULL
    BEGIN
        DECLARE @day NVARCHAR(6) = FORMAT(SYSUTCDATETIME(), 'yyMMdd');
        DECLARE @n INT = 1 + (SELECT COUNT(*) FROM [agent].[case]
                              WHERE [case_id] LIKE N'C-' + @day + N'-' + @style_id + N'-' + @market_id + N'-%');
        SET @case_id = CONCAT(N'C-', @day, N'-', @style_id, N'-', @market_id, N'-', RIGHT(N'0' + CAST(@n AS NVARCHAR(4)), 2));
        INSERT INTO [agent].[case] ([case_id], [trigger_id], [style_id], [market_id], [category], [lifecycle], [severity],
                                    [summary], [case_status], [evidence_json], [recommendation_json], [recommended_option_id])
        SELECT @case_id, @trigger_id, @style_id, @market_id, s.[category], s.[lifecycle], @severity, @summary, N'OPEN',
               @evidence_json, @recommendation_json, @recommended_option_id
        FROM (SELECT 1 AS k) x LEFT JOIN [erp].[product_style] s ON s.[style_id] = @style_id;
        SELECT @is_new = 1, @status = N'OPEN';
    END
    ELSE IF @status = N'DEFERRED' AND @defer <= SYSUTCDATETIME()
    BEGIN
        -- HL-04: the deferral expired and the trigger fired again -> refresh the evidence and re-open.
        UPDATE [agent].[case]
        SET [case_status] = N'AWAITING_APPROVAL', [severity] = @severity, [summary] = @summary, [evidence_json] = @evidence_json,
            [recommendation_json] = @recommendation_json, [recommended_option_id] = @recommended_option_id,
            [defer_until_utc] = NULL, [updated_at_utc] = SYSUTCDATETIME()
        WHERE [case_id] = @case_id;
        UPDATE [agent].[transfer_draft] SET [draft_status] = N'CANCELLED' WHERE [case_id] = @case_id AND [draft_status] = N'PROPOSED';
        SELECT @reopened = 1, @status = N'AWAITING_APPROVAL';
    END

    INSERT INTO [agent].[action_log] ([action_id], [case_id], [tier], [action_type], [actor], [outcome], [detail_json])
    VALUES (CONCAT(N'open:', @case_id, N':', CONVERT(NVARCHAR(36), NEWID())), @case_id, 1,
            CASE WHEN @is_new = 1 THEN N'AA-01_OPEN_CASE' WHEN @reopened = 1 THEN N'AA-01_REOPEN_CASE' ELSE N'AA-01_DEDUP' END,
            @actor, N'DONE', (SELECT @trigger_id AS trigger_id, @severity AS severity, @summary AS summary FOR JSON PATH, WITHOUT_ARRAY_WRAPPER));
    COMMIT;
    SELECT @case_id AS case_id, @is_new AS is_new, @reopened AS reopened, @status AS case_status, @chat AS chat_id;
END
GO

-- AA-04: stage draft transfer lines for one option. @lines_json: [{sku_id, from_location_id, to_location_id, units,
-- lane_mode, lead_days, cost_per_unit_usd, full_price_usd, unit_cost_usd}]. Replaces earlier PROPOSED lines.
CREATE OR ALTER PROCEDURE [agent].[usp_stage_transfer_draft]
    @case_id NVARCHAR(40), @option_id NVARCHAR(8), @lines_json NVARCHAR(MAX), @actor NVARCHAR(256) = N'agent'
AS
BEGIN
    SET NOCOUNT ON; SET XACT_ABORT ON;
    IF ISJSON(@lines_json) = 0 THROW 50003, N'lines_json is not valid JSON', 1;
    IF NOT EXISTS (SELECT 1 FROM [agent].[case] WHERE [case_id] = @case_id
                   AND [case_status] IN (N'OPEN', N'AWAITING_APPROVAL', N'DEFERRED'))
        THROW 50004, N'case is not open for staging', 1;
    BEGIN TRANSACTION;
    -- CANCELLED lines (from a re-open or re-simulation) are dropped too so line numbers can be reused; the action log keeps history.
    DELETE FROM [agent].[transfer_draft] WHERE [case_id] = @case_id AND [option_id] = @option_id
                                          AND [draft_status] IN (N'PROPOSED', N'CANCELLED');
    INSERT INTO [agent].[transfer_draft] ([case_id], [option_id], [line_no], [sku_id], [from_location_id], [to_location_id],
                                          [units], [lane_mode], [lead_days], [cost_per_unit_usd], [full_price_usd], [unit_cost_usd])
    SELECT @case_id, @option_id, ROW_NUMBER() OVER (ORDER BY j.[sku_id], j.[from_location_id]),
           j.[sku_id], j.[from_location_id], j.[to_location_id], j.[units], j.[lane_mode], j.[lead_days],
           j.[cost_per_unit_usd], j.[full_price_usd], j.[unit_cost_usd]
    FROM OPENJSON(@lines_json) WITH (
        [sku_id] NVARCHAR(32), [from_location_id] NVARCHAR(16), [to_location_id] NVARCHAR(16), [units] INT,
        [lane_mode] NVARCHAR(16), [lead_days] INT, [cost_per_unit_usd] DECIMAL(19,4), [full_price_usd] DECIMAL(19,4),
        [unit_cost_usd] DECIMAL(19,4)) j
    WHERE j.[units] > 0;
    DECLARE @rows INT = @@ROWCOUNT;
    INSERT INTO [agent].[action_log] ([action_id], [case_id], [tier], [action_type], [actor], [outcome], [detail_json])
    VALUES (CONCAT(N'stage:', @case_id, N':', @option_id, N':', CONVERT(NVARCHAR(36), NEWID())), @case_id, 1,
            N'AA-04_STAGE_DRAFT', @actor, N'DONE', (SELECT @option_id AS option_id, @rows AS lines FOR JSON PATH, WITHOUT_ARRAY_WRAPPER));
    COMMIT;
    SELECT @rows AS lines_staged;
END
GO

CREATE OR ALTER PROCEDURE [agent].[usp_set_case_chat]
    @case_id NVARCHAR(40), @chat_id NVARCHAR(256), @card_activity_id NVARCHAR(256) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    UPDATE [agent].[case]
    SET [chat_id] = @chat_id, [card_activity_id] = COALESCE(@card_activity_id, [card_activity_id]),
        [case_status] = CASE WHEN [case_status] = N'OPEN' THEN N'AWAITING_APPROVAL' ELSE [case_status] END,
        [updated_at_utc] = SYSUTCDATETIME()
    WHERE [case_id] = @case_id;
    SELECT CAST(@@ROWCOUNT AS INT) AS updated;
END
GO

-- CW-07 / HL-04: replace the recommendation and ALL proposed drafts atomically after a re-simulation (teammate
-- constraint, expired deferral). @drafts_json: [{option_id, lines: [<same line shape as usp_stage_transfer_draft>]}].
CREATE OR ALTER PROCEDURE [agent].[usp_update_recommendation]
    @case_id NVARCHAR(40), @recommendation_json NVARCHAR(MAX), @recommended_option_id NVARCHAR(8), @drafts_json NVARCHAR(MAX),
    @actor NVARCHAR(256), @action_type NVARCHAR(32) = N'CW-07_RESIMULATE', @detail_json NVARCHAR(MAX) = NULL,
    @evidence_json NVARCHAR(MAX) = NULL
AS
BEGIN
    SET NOCOUNT ON; SET XACT_ABORT ON;
    IF ISJSON(@recommendation_json) = 0 OR ISJSON(@drafts_json) = 0 OR (@detail_json IS NOT NULL AND ISJSON(@detail_json) = 0)
       OR (@evidence_json IS NOT NULL AND ISJSON(@evidence_json) = 0)
        THROW 50005, N'recommendation/drafts/detail/evidence must be JSON', 1;
    BEGIN TRANSACTION;
    UPDATE [agent].[case]
    SET [recommendation_json] = @recommendation_json, [recommended_option_id] = @recommended_option_id,
        [evidence_json] = COALESCE(@evidence_json, [evidence_json]), [updated_at_utc] = SYSUTCDATETIME()
    WHERE [case_id] = @case_id AND [case_status] IN (N'OPEN', N'AWAITING_APPROVAL', N'DEFERRED');
    IF @@ROWCOUNT = 0 THROW 50004, N'case is not open for re-simulation', 1;
    DELETE FROM [agent].[transfer_draft] WHERE [case_id] = @case_id AND [draft_status] IN (N'PROPOSED', N'CANCELLED');
    INSERT INTO [agent].[transfer_draft] ([case_id], [option_id], [line_no], [sku_id], [from_location_id], [to_location_id],
                                          [units], [lane_mode], [lead_days], [cost_per_unit_usd], [full_price_usd], [unit_cost_usd])
    SELECT @case_id, o.[option_id], ROW_NUMBER() OVER (PARTITION BY o.[option_id] ORDER BY j.[sku_id], j.[from_location_id]),
           j.[sku_id], j.[from_location_id], j.[to_location_id], j.[units], j.[lane_mode], j.[lead_days],
           j.[cost_per_unit_usd], j.[full_price_usd], j.[unit_cost_usd]
    FROM OPENJSON(@drafts_json) WITH ([option_id] NVARCHAR(8), [lines] NVARCHAR(MAX) AS JSON) o
    CROSS APPLY OPENJSON(o.[lines]) WITH (
        [sku_id] NVARCHAR(32), [from_location_id] NVARCHAR(16), [to_location_id] NVARCHAR(16), [units] INT,
        [lane_mode] NVARCHAR(16), [lead_days] INT, [cost_per_unit_usd] DECIMAL(19,4), [full_price_usd] DECIMAL(19,4),
        [unit_cost_usd] DECIMAL(19,4)) j
    WHERE j.[units] > 0;
    DECLARE @rows INT = @@ROWCOUNT;
    INSERT INTO [agent].[action_log] ([action_id], [case_id], [tier], [action_type], [actor], [outcome], [detail_json])
    VALUES (CONCAT(N'resim:', @case_id, N':', CONVERT(NVARCHAR(36), NEWID())), @case_id, 1, @action_type, @actor, N'DONE',
            JSON_MODIFY(COALESCE(@detail_json, N'{}'), '$.lines_staged', @rows));
    COMMIT;
    SELECT @rows AS lines_staged;
END
GO

CREATE OR ALTER PROCEDURE [agent].[usp_add_case_member]
    @case_id NVARCHAR(40), @person_id NVARCHAR(16), @upn NVARCHAR(256), @case_role NVARCHAR(32)
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @inserted INT = 0;
    IF NOT EXISTS (SELECT 1 FROM [agent].[case_member] WHERE [case_id] = @case_id AND [person_id] = @person_id AND [case_role] = @case_role)
    BEGIN
        INSERT INTO [agent].[case_member] ([case_id], [person_id], [upn], [case_role]) VALUES (@case_id, @person_id, @upn, @case_role);
        SET @inserted = @@ROWCOUNT;
    END
    SELECT @inserted AS inserted;
END
GO

-- AA-02: people to pull into the case chat. Destination-market owners decide; source-market owners are consulted
-- (their stock moves). DQ-12: no accountable owner -> the escalation delegate is added instead.
CREATE OR ALTER PROCEDURE [agent].[usp_get_case_owners] @case_id NVARCHAR(40)
AS
BEGIN
    SET NOCOUNT ON;
    WITH c AS (SELECT [category], [market_id] FROM [agent].[case] WHERE [case_id] = @case_id),
    src AS (SELECT DISTINCT l.[market_id] FROM [agent].[transfer_draft] d
            JOIN [erp].[location] l ON l.[location_id] = d.[from_location_id]
            WHERE d.[case_id] = @case_id AND d.[draft_status] = N'PROPOSED'
              AND l.[market_id] <> (SELECT [market_id] FROM c)),
    r AS (
        SELECT o.[role] AS case_role, o.[person_id], o.[approval_limit_units], o.[market_id], CAST(NULL AS NVARCHAR(16)) AS location_id
        FROM c JOIN [ref].[decision_owner] o ON o.[category] = c.[category] AND o.[market_id] = c.[market_id]
        UNION ALL
        SELECT N'SOURCE_' + o.[role], o.[person_id], NULL, o.[market_id], NULL
        FROM c JOIN [ref].[decision_owner] o ON o.[category] = c.[category]
        JOIN src ON src.[market_id] = o.[market_id]
        WHERE o.[role] IN (N'ACCOUNTABLE', N'PLANNER')
        UNION ALL
        SELECT N'ESCALATION', p.[person_id], 5000, c.[market_id], NULL
        FROM c CROSS JOIN [agent].[persona_user] p
        WHERE p.[is_escalation] = 1 AND NOT EXISTS (
            SELECT 1 FROM [ref].[decision_owner] o
            WHERE o.[category] = c.[category] AND o.[market_id] = c.[market_id] AND o.[role] = N'ACCOUNTABLE')
        UNION ALL
        -- Demo operator (agent_users_group.demo_operator): may take every decision on every case; tagged in the audit trail.
        SELECT N'OPERATOR', p.[person_id], NULL, c.[market_id], NULL
        FROM c CROSS JOIN [agent].[persona_user] p WHERE p.[person_id] = N'operator'
        UNION ALL
        -- Store extension: managers of the stores that send or receive units in the recommended option.
        SELECT DISTINCT CASE WHEN sm.[location_id] = d.[from_location_id] THEN N'STORE_MANAGER_SOURCE' ELSE N'STORE_MANAGER_DEST' END,
               sm.[person_id], NULL, (SELECT [market_id] FROM c), sm.[location_id]
        FROM [agent].[transfer_draft] d
        JOIN [agent].[case] k ON k.[case_id] = d.[case_id] AND k.[recommended_option_id] = d.[option_id]
        JOIN [ref].[store_manager] sm ON sm.[location_id] IN (d.[from_location_id], d.[to_location_id])
        WHERE d.[case_id] = @case_id AND d.[draft_status] = N'PROPOSED')
    SELECT r.case_role, r.person_id, r.approval_limit_units, r.market_id, r.location_id, p.[upn], p.[aad_object_id],
           COALESCE(rp.[display_name], p.[display_name]) AS persona_name, rp.[title], p.[display_name] AS user_display_name
    FROM r LEFT JOIN [agent].[persona_user] p ON p.[person_id] = r.person_id
    LEFT JOIN [ref].[person] rp ON rp.[person_id] = r.person_id
    ORDER BY CASE WHEN r.case_role IN (N'ACCOUNTABLE', N'ESCALATION') THEN 0 ELSE 1 END, r.case_role, r.person_id;
END
GO

CREATE OR ALTER PROCEDURE [agent].[usp_get_case] @case_id NVARCHAR(40)
AS
BEGIN
    SET NOCOUNT ON;
    SELECT (
        SELECT c.[case_id], c.[trigger_id], c.[style_id], c.[market_id], c.[category], c.[lifecycle], c.[severity], c.[summary],
               c.[case_status], c.[chat_id], c.[card_activity_id], c.[recommended_option_id], c.[defer_until_utc], c.[decision_id],
               c.[pending_option_id], c.[pending_units], c.[pending_approver_upn], c.[opened_at_utc], c.[updated_at_utc],
               JSON_QUERY(c.[evidence_json]) AS evidence, JSON_QUERY(c.[recommendation_json]) AS recommendation,
               (SELECT d.[option_id], d.[line_no], d.[sku_id], d.[from_location_id], d.[to_location_id], d.[units], d.[released_units],
                       d.[lane_mode], d.[lead_days], d.[cost_per_unit_usd], d.[full_price_usd], d.[unit_cost_usd], d.[draft_status]
                FROM [agent].[transfer_draft] d WHERE d.[case_id] = c.[case_id] ORDER BY d.[option_id], d.[line_no] FOR JSON PATH) AS drafts,
               (SELECT m.[person_id], m.[upn], m.[case_role] FROM [agent].[case_member] m WHERE m.[case_id] = c.[case_id] FOR JSON PATH) AS members,
               (SELECT TOP 20 a.[action_id], a.[tier], a.[action_type], a.[actor], a.[outcome], a.[created_at_utc], JSON_QUERY(a.[detail_json]) AS detail
                FROM [agent].[action_log] a WHERE a.[case_id] = c.[case_id] ORDER BY a.[created_at_utc] DESC FOR JSON PATH) AS actions
        FROM [agent].[case] c WHERE c.[case_id] = @case_id
        FOR JSON PATH, WITHOUT_ARRAY_WRAPPER) AS case_json;
END
GO

-- AA-05 and HL-04: follow-ups that are due. The agent's timer re-opens expired deferrals and runs T+7/T+14 checks.
CREATE OR ALTER PROCEDURE [agent].[usp_claim_due_follow_ups] @max_rows INT = 20
AS
BEGIN
    SET NOCOUNT ON; SET XACT_ABORT ON;
    DECLARE @claimed TABLE ([follow_up_id] INT, [case_id] NVARCHAR(40), [task_type] NVARCHAR(24), [due_at_utc] DATETIME2(0));
    BEGIN TRANSACTION;
    UPDATE TOP (@max_rows) f SET [task_status] = N'DONE', [completed_at_utc] = SYSUTCDATETIME()
    OUTPUT inserted.[follow_up_id], inserted.[case_id], inserted.[task_type], inserted.[due_at_utc] INTO @claimed
    FROM [agent].[follow_up] f WITH (UPDLOCK, READPAST)
    WHERE f.[task_status] = N'PENDING' AND f.[due_at_utc] <= SYSUTCDATETIME();
    UPDATE c SET [case_status] = N'AWAITING_APPROVAL', [defer_until_utc] = NULL, [updated_at_utc] = SYSUTCDATETIME()
    FROM [agent].[case] c JOIN @claimed x ON x.[case_id] = c.[case_id]
    WHERE x.[task_type] = N'REOPEN_DEFERRED' AND c.[case_status] = N'DEFERRED';
    COMMIT;
    SELECT x.[follow_up_id], x.[case_id], x.[task_type], x.[due_at_utc], c.[chat_id], c.[case_status]
    FROM @claimed x JOIN [agent].[case] c ON c.[case_id] = x.[case_id];
END
GO

------------------------------------------------------------------------------------------------------------------
-- Tier 2 action gateway (requirements §8.2 gateway contract, §9 HL-02..HL-11, routing matrix)
------------------------------------------------------------------------------------------------------------------
-- @decision: APPROVE (option as staged) | REVISE (fewer units, HL-03) | OVERRIDE (non-recommended option, rationale
-- + reason code mandatory, HL-06) | REJECT (HL-07) | DEFER (needs @defer_until_utc, HL-04).
-- Returns one row: outcome (EXECUTED | PENDING_FINANCE | DEFERRED | REJECTED | BLOCKED | DUPLICATE), message, case_status,
-- decision_id, sto_id, approved_units, gm_usd, transfer_cost_usd.
CREATE OR ALTER PROCEDURE [agent].[usp_record_decision]
    @action_id NVARCHAR(64), @case_id NVARCHAR(40), @decision NVARCHAR(16), @approver_upn NVARCHAR(256),
    @option_id NVARCHAR(8) = NULL, @units INT = NULL, @rationale NVARCHAR(2000) = NULL, @reason_code NVARCHAR(32) = NULL,
    @defer_until_utc DATETIME2(0) = NULL, @materiality_gm_usd DECIMAL(19,2) = 50000
AS
BEGIN
    SET NOCOUNT ON; SET XACT_ABORT ON;
    DECLARE @outcome NVARCHAR(24), @msg NVARCHAR(1000), @decision_id UNIQUEIDENTIFIER, @sto_id NVARCHAR(32),
            @final_units INT = 0, @gm DECIMAL(19,2) = 0, @tcost DECIMAL(19,2) = 0, @new_status NVARCHAR(24);

    -- Gateway contract #4: idempotency.
    DECLARE @prior NVARCHAR(MAX) = (SELECT [detail_json] FROM [agent].[action_log] WHERE [action_id] = @action_id);
    IF @prior IS NOT NULL
    BEGIN
        SELECT N'DUPLICATE' AS outcome, CONCAT(N'Already recorded: ', JSON_VALUE(@prior, '$.outcome'), N'. ', JSON_VALUE(@prior, '$.message')) AS message,
               JSON_VALUE(@prior, '$.case_status') AS case_status, TRY_CAST(JSON_VALUE(@prior, '$.decision_id') AS UNIQUEIDENTIFIER) AS decision_id,
               JSON_VALUE(@prior, '$.sto_id') AS sto_id, TRY_CAST(JSON_VALUE(@prior, '$.approved_units') AS INT) AS approved_units,
               TRY_CAST(JSON_VALUE(@prior, '$.gm_usd') AS DECIMAL(19,2)) AS gm_usd,
               TRY_CAST(JSON_VALUE(@prior, '$.transfer_cost_usd') AS DECIMAL(19,2)) AS transfer_cost_usd;
        RETURN;
    END

    SET @decision = UPPER(LTRIM(RTRIM(@decision)));
    BEGIN TRANSACTION;

    DECLARE @status NVARCHAR(24), @style NVARCHAR(16), @market NVARCHAR(16), @category NVARCHAR(16), @lifecycle NVARCHAR(16),
            @trigger NVARCHAR(8), @severity NVARCHAR(16), @summary NVARCHAR(1000), @opened DATETIME2(0), @rec NVARCHAR(MAX),
            @rec_opt NVARCHAR(8), @p_dec NVARCHAR(16), @p_opt NVARCHAR(8), @p_units INT, @p_upn NVARCHAR(256), @p_person NVARCHAR(16),
            @p_rat NVARCHAR(2000), @p_reason NVARCHAR(32), @evidence NVARCHAR(MAX);
    SELECT @status = [case_status], @style = [style_id], @market = [market_id], @category = [category], @lifecycle = [lifecycle],
           @trigger = [trigger_id], @severity = [severity], @summary = [summary], @opened = [opened_at_utc], @rec = [recommendation_json],
           @rec_opt = [recommended_option_id], @p_dec = [pending_decision], @p_opt = [pending_option_id], @p_units = [pending_units],
           @p_upn = [pending_approver_upn], @p_person = [pending_approver_person_id], @p_rat = [pending_rationale],
           @p_reason = [pending_reason_code], @evidence = [evidence_json]
    FROM [agent].[case] WITH (UPDLOCK, HOLDLOCK) WHERE [case_id] = @case_id;

    -- Gateway contract #1: the case exists and is awaiting a decision.
    IF @status IS NULL BEGIN SET @msg = N'Case not found.'; GOTO blocked; END
    IF @status NOT IN (N'OPEN', N'AWAITING_APPROVAL', N'NEEDS_FINANCE', N'DEFERRED')
    BEGIN SET @msg = CONCAT(N'Case is ', @status, N'; no further decisions are accepted.'); GOTO blocked; END
    IF @decision NOT IN (N'APPROVE', N'REVISE', N'OVERRIDE', N'REJECT', N'DEFER')
    BEGIN SET @msg = N'Unknown decision.'; GOTO blocked; END

    -- Gateway contract #2: approver role (HL-09, HL-11). Several personas may map to one demo user, so take the strongest role.
    DECLARE @acc_person NVARCHAR(16), @acc_limit INT, @fin_person NVARCHAR(16), @is_escalation BIT = 0;
    SELECT TOP 1 @acc_person = o.[person_id], @acc_limit = o.[approval_limit_units]
    FROM [ref].[decision_owner] o JOIN [agent].[persona_user] p ON p.[person_id] = o.[person_id]
    WHERE o.[role] = N'ACCOUNTABLE' AND o.[category] = @category AND o.[market_id] = @market AND p.[upn] = @approver_upn
    ORDER BY o.[approval_limit_units] DESC;
    IF @acc_person IS NULL AND NOT EXISTS (SELECT 1 FROM [ref].[decision_owner]
                                           WHERE [role] = N'ACCOUNTABLE' AND [category] = @category AND [market_id] = @market)
        SELECT TOP 1 @acc_person = [person_id], @acc_limit = 5000, @is_escalation = 1
        FROM [agent].[persona_user] WHERE [is_escalation] = 1 AND [upn] = @approver_upn;
    SELECT TOP 1 @fin_person = o.[person_id]
    FROM [ref].[decision_owner] o JOIN [agent].[persona_user] p ON p.[person_id] = o.[person_id]
    WHERE o.[role] = N'FINANCE' AND o.[category] = @category AND o.[market_id] = @market AND p.[upn] = @approver_upn;
    -- Demo operator: stands in for the accountable owner (with the market's highest owner limit) and for Finance.
    -- Scope, stock, guardrail and materiality checks below still apply.
    DECLARE @is_operator BIT = CASE WHEN EXISTS (SELECT 1 FROM [agent].[persona_user]
                                                 WHERE [person_id] = N'operator' AND [upn] = @approver_upn) THEN 1 ELSE 0 END;
    IF @is_operator = 1 AND @acc_person IS NULL
        SELECT @acc_person = N'operator', @acc_limit = COALESCE(MAX([approval_limit_units]), 5000)
        FROM [ref].[decision_owner] WHERE [role] = N'ACCOUNTABLE' AND [category] = @category AND [market_id] = @market;
    IF @is_operator = 1 AND @fin_person IS NULL SET @fin_person = N'operator';

    DECLARE @approver_person NVARCHAR(16) = @acc_person, @co_upn NVARCHAR(256), @co_person NVARCHAR(16), @label NVARCHAR(16);

    IF @status = N'NEEDS_FINANCE'
    BEGIN
        -- Routing matrix: transfer with GM$ above the materiality threshold = Director + Finance.
        IF @decision = N'REJECT' AND (@fin_person IS NOT NULL OR @acc_person IS NOT NULL)
        BEGIN
            SET @approver_person = COALESCE(@fin_person, @acc_person);
            GOTO do_reject;
        END
        IF @fin_person IS NULL OR @decision <> N'APPROVE'
        BEGIN SET @msg = N'Awaiting Finance co-approval (HL-05): only a FINANCE owner can approve, or Finance/the owner can reject.'; GOTO blocked; END
        SELECT @co_upn = @approver_upn, @co_person = @fin_person, @decision = @p_dec, @option_id = @p_opt, @units = @p_units,
               @approver_upn = @p_upn, @approver_person = @p_person, @rationale = @p_rat, @reason_code = @p_reason;
        SELECT @acc_limit = MAX([approval_limit_units]) FROM [ref].[decision_owner]
        WHERE [role] = N'ACCOUNTABLE' AND [category] = @category AND [market_id] = @market AND [person_id] = @approver_person;
        IF @acc_limit IS NULL SET @acc_limit = 5000;  -- escalation delegate
        GOTO plan_transfer;
    END

    IF @acc_person IS NULL
    BEGIN
        SET @msg = CONCAT(N'Only the accountable owner for ', @category, N' / ', @market,
                          N' can record a decision (HL-11). Teammates can comment or propose a revision in the chat.');
        GOTO blocked;
    END

    IF @decision = N'DEFER'
    BEGIN
        IF @defer_until_utc IS NULL OR @defer_until_utc <= SYSUTCDATETIME()
        BEGIN SET @msg = N'Defer needs a future defer-until date (HL-04).'; GOTO blocked; END
        UPDATE [agent].[case] SET [case_status] = N'DEFERRED', [defer_until_utc] = @defer_until_utc, [updated_at_utc] = SYSUTCDATETIME()
        WHERE [case_id] = @case_id;
        INSERT INTO [agent].[follow_up] ([case_id], [task_type], [due_at_utc]) VALUES (@case_id, N'REOPEN_DEFERRED', @defer_until_utc);
        SELECT @outcome = N'DEFERRED', @new_status = N'DEFERRED',
               @msg = CONCAT(N'Deferred until ', CONVERT(NVARCHAR(20), @defer_until_utc, 120), N' UTC. The case re-opens automatically.');
        GOTO done;
    END

    IF @decision = N'REJECT' GOTO do_reject;

    IF @decision IN (N'REVISE', N'OVERRIDE') AND NULLIF(LTRIM(@rationale), N'') IS NULL
    BEGIN SET @msg = N'Revise and Override need a business rationale (HL-03, HL-06).'; GOTO blocked; END
    IF @decision = N'OVERRIDE' AND NULLIF(LTRIM(@reason_code), N'') IS NULL
    BEGIN SET @msg = N'Override needs a reason code (HL-06).'; GOTO blocked; END
    IF @option_id IS NULL SET @option_id = @rec_opt;

plan_transfer:
    DECLARE @lines TABLE ([line_no] INT, [sku_id] NVARCHAR(32), [from_location_id] NVARCHAR(16), [to_location_id] NVARCHAR(16),
                          [units] INT, [cost] DECIMAL(19,4), [price] DECIMAL(19,4), [ucost] DECIMAL(19,4));
    DECLARE @draft_units INT = (SELECT SUM([units]) FROM [agent].[transfer_draft]
                                WHERE [case_id] = @case_id AND [option_id] = @option_id AND [draft_status] = N'PROPOSED');

    IF @draft_units IS NULL
    BEGIN
        IF @option_id = N'S0'
        BEGIN
            -- "Maintain allocation": record the decision, nothing moves.
            SELECT @final_units = 0, @label = CASE WHEN @rec_opt = N'S0' THEN N'APPROVED' ELSE N'MODIFIED' END;
            GOTO write_decision;
        END
        SET @msg = CONCAT(N'Option ', @option_id, N' has no staged draft transfer; ask the agent to re-simulate.'); GOTO blocked;
    END

    SET @final_units = COALESCE(@units, @draft_units);
    -- Gateway contract #3: within the staged scope. A larger quantity must be re-simulated and re-staged first (HL-03).
    IF @final_units <= 0 OR @final_units > @draft_units
    BEGIN SET @msg = CONCAT(N'Units must be between 1 and the staged ', @draft_units, N' for option ', @option_id, N'.'); GOTO blocked; END
    IF @final_units > @acc_limit
    BEGIN SET @msg = CONCAT(N'Units ', @final_units, N' exceed the approver limit of ', @acc_limit, N'.'); GOTO blocked; END

    -- Scale the staged lines to the approved total (largest remainder keeps the size curve).
    ;WITH d AS (
        SELECT [line_no], [sku_id], [from_location_id], [to_location_id], [cost_per_unit_usd], [full_price_usd], [unit_cost_usd],
               CAST([units] AS DECIMAL(19,6)) * @final_units / @draft_units AS exact
        FROM [agent].[transfer_draft] WHERE [case_id] = @case_id AND [option_id] = @option_id AND [draft_status] = N'PROPOSED'),
    f AS (SELECT *, CAST(FLOOR(exact) AS INT) AS base, ROW_NUMBER() OVER (ORDER BY exact - FLOOR(exact) DESC, [line_no]) AS rk FROM d)
    INSERT INTO @lines
    SELECT [line_no], [sku_id], [from_location_id], [to_location_id],
           base + CASE WHEN rk <= @final_units - (SELECT SUM(base) FROM f) THEN 1 ELSE 0 END,
           [cost_per_unit_usd], [full_price_usd], [unit_cost_usd]
    FROM f;
    DELETE FROM @lines WHERE [units] <= 0;

    -- DC availability at the source (on hand - allocated - reserved).
    DECLARE @short NVARCHAR(200) = (
        SELECT TOP 1 CONCAT(l.[sku_id], N' at ', l.[from_location_id], N': needs ', l.[units], N', available ',
                            ISNULL(i.[on_hand_units], 0) - ISNULL(i.[allocated_units], 0) - ISNULL(i.[reserved_units], 0))
        FROM @lines l LEFT JOIN [wms].[inventory_position] i ON i.[location_id] = l.[from_location_id] AND i.[sku_id] = l.[sku_id]
        WHERE ISNULL(i.[on_hand_units], 0) - ISNULL(i.[allocated_units], 0) - ISNULL(i.[reserved_units], 0) < l.[units]);
    IF @short IS NOT NULL BEGIN SET @msg = CONCAT(N'Source location (DC or donor store) no longer has the stock: ', @short, N'.'); GOTO blocked; END

    -- Margin guardrail: lane cost / destination full price <= max_transfer_cost_pct (same rule as gold.mart_rebalancing_pairs).
    DECLARE @cap DECIMAL(19,4) = (SELECT [max_transfer_cost_pct] FROM [erp].[margin_guardrail]
                                  WHERE [market_id] = @market AND [category] = @category AND [lifecycle] = @lifecycle);
    IF @cap IS NULL BEGIN SET @msg = N'No margin guardrail configured for this market, category and lifecycle.'; GOTO blocked; END
    DECLARE @worst DECIMAL(19,4) = (SELECT MAX([cost] / NULLIF([price], 0)) FROM @lines);
    IF @worst IS NULL OR @worst > @cap
    BEGIN SET @msg = CONCAT(N'Transfer cost is ', FORMAT(@worst, 'P1'), N' of full price; the guardrail is ', FORMAT(@cap, 'P1'), N'.'); GOTO blocked; END

    SELECT @gm = SUM([units] * ([price] - [ucost] - [cost])), @tcost = SUM([units] * [cost]) FROM @lines;
    SET @label = CASE WHEN @decision = N'APPROVE' AND @final_units = @draft_units AND @option_id = ISNULL(@rec_opt, @option_id)
                      THEN N'APPROVED' ELSE N'MODIFIED' END;

    -- HL-05 materiality: the owner's approval is recorded, Finance must co-approve before anything is written.
    IF @co_upn IS NULL AND @gm > @materiality_gm_usd
    BEGIN
        UPDATE [agent].[case]
        SET [case_status] = N'NEEDS_FINANCE', [pending_decision] = @decision, [pending_option_id] = @option_id, [pending_units] = @final_units,
            [pending_approver_upn] = @approver_upn, [pending_approver_person_id] = @approver_person, [pending_rationale] = @rationale,
            [pending_reason_code] = @reason_code, [updated_at_utc] = SYSUTCDATETIME()
        WHERE [case_id] = @case_id;
        SELECT @outcome = N'PENDING_FINANCE', @new_status = N'NEEDS_FINANCE',
               @msg = CONCAT(N'Owner approval recorded. GM impact $', FORMAT(@gm, 'N0'), N' is above the $', FORMAT(@materiality_gm_usd, 'N0'),
                             N' materiality threshold, so Finance must co-approve (HL-05).');
        GOTO done;
    END
    GOTO write_decision;

do_reject:
    SELECT @label = N'REJECTED', @final_units = 0, @option_id = N'S0';

write_decision:
    SET @decision_id = NEWID();
    DECLARE @case_obj NVARCHAR(MAX) = (
        SELECT @trigger AS trigger_id, COALESCE((SELECT TOP 1 [sku_id] FROM @lines ORDER BY [units] DESC), JSON_VALUE(@evidence, '$.sku_id')) AS sku_id,
               @severity AS severity, CONVERT(NVARCHAR(20), @opened, 127) + N'Z' AS opened_at, @summary AS summary, @case_id AS case_id
        FOR JSON PATH, WITHOUT_ARRAY_WRAPPER);
    DECLARE @ev NVARCHAR(MAX) = N'{}';
    SET @ev = JSON_MODIFY(@ev, '$.case', JSON_QUERY(@case_obj));
    SET @ev = JSON_MODIFY(@ev, '$.reason_code', COALESCE(@reason_code,
              CASE @label WHEN N'APPROVED' THEN N'AGENT_RECOMMENDATION' WHEN N'MODIFIED' THEN N'OWNER_REVISION' ELSE N'OWNER_REJECTED' END));
    SET @ev = JSON_MODIFY(@ev, '$.chosen_option_id', @option_id);
    SET @ev = JSON_MODIFY(@ev, '$.approved_units', @final_units);
    SET @ev = JSON_MODIFY(@ev, '$.recommended_option_id', @rec_opt);
    SET @ev = JSON_MODIFY(@ev, '$.options', JSON_QUERY(@rec, '$.options'));
    SET @ev = JSON_MODIFY(@ev, '$.gateway', JSON_QUERY((
        SELECT @action_id AS action_id, @approver_upn AS approver_upn, @co_upn AS finance_co_approver_upn, @gm AS gm_usd,
               @tcost AS transfer_cost_usd, @cap AS max_transfer_cost_pct, @worst AS transfer_cost_pct_of_price
        FOR JSON PATH, WITHOUT_ARRAY_WRAPPER)));

    INSERT INTO [erp].[allocation_decision] ([decision_id], [proposal_id], [style_id], [market_id], [decision], [approved_by_upn],
                                             [approver_person_id], [rationale], [evidence_json])
    VALUES (@decision_id, @case_id, @style, @market, @label, @approver_upn, @approver_person,
            CONCAT(@rationale, CASE WHEN @co_upn IS NOT NULL THEN CONCAT(N' [Finance co-approval: ', @co_upn, N']') ELSE N'' END,
                   CASE WHEN @is_escalation = 1 THEN N' [Escalation delegate: no accountable owner, DQ-12]' ELSE N'' END,
                   CASE WHEN @is_operator = 1 AND (@approver_person = N'operator' OR @co_person = N'operator')
                        THEN N' [Demo operator]' ELSE N'' END),
            @ev);

    IF @final_units > 0
    BEGIN
        SET @sto_id = LEFT(CONCAT(N'STO-', SUBSTRING(@case_id, 3, 40)), 32);
        INSERT INTO [erp].[stock_transfer_order] ([sto_id], [line_no], [decision_id], [sku_id], [from_location_id], [to_location_id],
                                                  [units], [sto_status], [requested_ship_date])
        SELECT @sto_id, ROW_NUMBER() OVER (ORDER BY [line_no]), @decision_id, [sku_id], [from_location_id], [to_location_id], [units],
               N'CREATED', CAST(SYSUTCDATETIME() AS DATE)
        FROM @lines;
        UPDATE i SET [reserved_units] = ISNULL(i.[reserved_units], 0) + l.[units], [snapshot_ts] = SYSUTCDATETIME()
        FROM [wms].[inventory_position] i JOIN @lines l ON l.[from_location_id] = i.[location_id] AND l.[sku_id] = i.[sku_id];
        UPDATE d SET [draft_status] = N'RELEASED', [released_units] = l.[units]
        FROM [agent].[transfer_draft] d JOIN @lines l ON l.[line_no] = d.[line_no]
        WHERE d.[case_id] = @case_id AND d.[option_id] = @option_id AND d.[draft_status] = N'PROPOSED';
        INSERT INTO [agent].[follow_up] ([case_id], [task_type], [due_at_utc])
        VALUES (@case_id, N'CHECK_T7', DATEADD(DAY, 7, SYSUTCDATETIME())), (@case_id, N'CHECK_T14', DATEADD(DAY, 14, SYSUTCDATETIME()));
    END
    UPDATE [agent].[transfer_draft] SET [draft_status] = N'CANCELLED' WHERE [case_id] = @case_id AND [draft_status] = N'PROPOSED';
    SET @new_status = CASE WHEN @label = N'REJECTED' THEN N'REJECTED' ELSE N'APPROVED' END;
    UPDATE [agent].[case]
    SET [case_status] = @new_status, [decision_id] = @decision_id, [pending_decision] = NULL, [pending_option_id] = NULL,
        [pending_units] = NULL, [pending_approver_upn] = NULL, [pending_approver_person_id] = NULL, [pending_rationale] = NULL,
        [pending_reason_code] = NULL, [updated_at_utc] = SYSUTCDATETIME()
    WHERE [case_id] = @case_id;
    SELECT @outcome = CASE WHEN @label = N'REJECTED' THEN N'REJECTED' ELSE N'EXECUTED' END,
           @msg = CASE WHEN @label = N'REJECTED' THEN N'Recommendation rejected and recorded in ERP.'
                       WHEN @final_units = 0 THEN N'Decision recorded in ERP: maintain allocation, nothing moves.'
                       ELSE CONCAT(N'Execution complete: ', @final_units, N' units on ', @sto_id, N', reserved at the source DC.') END;

done:
    INSERT INTO [agent].[action_log] ([action_id], [case_id], [tier], [action_type], [actor], [outcome], [detail_json])
    VALUES (@action_id, @case_id, 2, CONCAT(N'AC-01_', @decision), COALESCE(@co_upn, @approver_upn), @outcome,
            (SELECT @outcome AS outcome, @msg AS message, @new_status AS case_status, @decision_id AS decision_id, @sto_id AS sto_id,
                    @option_id AS option_id, @final_units AS approved_units, @gm AS gm_usd, @tcost AS transfer_cost_usd,
                    @approver_upn AS approver_upn, @co_upn AS finance_co_approver_upn
             FOR JSON PATH, WITHOUT_ARRAY_WRAPPER));
    COMMIT;
    SELECT @outcome AS outcome, @msg AS message, @new_status AS case_status, @decision_id AS decision_id, @sto_id AS sto_id,
           @final_units AS approved_units, @gm AS gm_usd, @tcost AS transfer_cost_usd;
    RETURN;

blocked:
    IF @@TRANCOUNT > 0 ROLLBACK;
    -- Blocked attempts are audited but do not consume the idempotency key, so a corrected retry can succeed.
    INSERT INTO [agent].[action_log] ([action_id], [case_id], [tier], [action_type], [actor], [outcome], [detail_json])
    VALUES (LEFT(CONCAT(@action_id, N':blocked:', CONVERT(NVARCHAR(36), NEWID())), 80),
            CASE WHEN EXISTS (SELECT 1 FROM [agent].[case] WHERE [case_id] = @case_id) THEN @case_id END, 2,
            CONCAT(N'AC-01_', LEFT(@decision, 20)), @approver_upn, N'BLOCKED',
            (SELECT @msg AS message, @option_id AS option_id, @units AS units FOR JSON PATH, WITHOUT_ARRAY_WRAPPER));
    SELECT N'BLOCKED' AS outcome, @msg AS message, (SELECT [case_status] FROM [agent].[case] WHERE [case_id] = @case_id) AS case_status,
           CAST(NULL AS UNIQUEIDENTIFIER) AS decision_id, CAST(NULL AS NVARCHAR(32)) AS sto_id, CAST(NULL AS INT) AS approved_units,
           @gm AS gm_usd, @tcost AS transfer_cost_usd;
END
GO
