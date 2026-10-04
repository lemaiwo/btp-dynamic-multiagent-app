CREATE TABLE ide_sessions (
	id VARCHAR(36) NOT NULL, 
	owner VARCHAR(255) NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	target VARCHAR(64) NOT NULL, 
	session_type VARCHAR(16) DEFAULT 'change' NOT NULL, 
	stage VARCHAR(16) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	run_id VARCHAR(36), 
	requests_used INTEGER NOT NULL, 
	todos_json TEXT, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_ide_sessions_owner ON ide_sessions (owner);
CREATE TABLE ide_conventions (
	target VARCHAR(64) NOT NULL, 
	label VARCHAR(120) NOT NULL, 
	destination VARCHAR(200) NOT NULL, 
	namespace VARCHAR(30) NOT NULL, 
	package VARCHAR(30) NOT NULL, 
	atc_variant VARCHAR(30) NOT NULL, 
	clean_core_level VARCHAR(1) NOT NULL, 
	free_text TEXT NOT NULL, 
	non_production BOOLEAN DEFAULT 0 NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (target)
);
CREATE TABLE ide_audit_log (
	id VARCHAR(36) NOT NULL, 
	ts DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	principal VARCHAR(255) NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	target VARCHAR(64) NOT NULL, 
	action VARCHAR(32) NOT NULL, 
	params_json TEXT NOT NULL, 
	outcome VARCHAR(16) NOT NULL, 
	request_id VARCHAR(100), 
	PRIMARY KEY (id)
);
CREATE INDEX ix_ide_audit_log_ts ON ide_audit_log (ts);
CREATE INDEX ix_ide_audit_log_session_id ON ide_audit_log (session_id);
CREATE TABLE ide_messages (
	id VARCHAR(36) NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	stage VARCHAR(16) NOT NULL, 
	role VARCHAR(16) NOT NULL, 
	content TEXT NOT NULL, 
	activity_json TEXT, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES ide_sessions (id) ON DELETE CASCADE
);
CREATE INDEX ix_ide_messages_session_id ON ide_messages (session_id);
CREATE TABLE ide_artifacts (
	id VARCHAR(36) NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	stage VARCHAR(16) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	content TEXT NOT NULL, 
	version INTEGER NOT NULL, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES ide_sessions (id) ON DELETE CASCADE
);
CREATE INDEX ix_ide_artifacts_session_id ON ide_artifacts (session_id);
CREATE TABLE ide_workspace_files (
	id VARCHAR(36) NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	path VARCHAR(200) NOT NULL, 
	object_type VARCHAR(8), 
	object_name VARCHAR(40), 
	origin_source TEXT, 
	proposed_source TEXT, 
	state VARCHAR(8) NOT NULL, 
	lint_json TEXT, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_ide_workspace_file_path UNIQUE (session_id, path), 
	FOREIGN KEY(session_id) REFERENCES ide_sessions (id) ON DELETE CASCADE
);
CREATE INDEX ix_ide_workspace_files_session_id ON ide_workspace_files (session_id);
CREATE TABLE ide_findings (
	id VARCHAR(36) NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	ref_id VARCHAR(255) NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	program VARCHAR(40), 
	include VARCHAR(40), 
	line INTEGER, 
	occurred_at VARCHAR(32), 
	detail TEXT, 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_ide_finding_ref UNIQUE (session_id, kind, ref_id), 
	FOREIGN KEY(session_id) REFERENCES ide_sessions (id) ON DELETE CASCADE
);
CREATE INDEX ix_ide_findings_session_id ON ide_findings (session_id);
CREATE TABLE ide_approvals (
	id VARCHAR(36) NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(36), 
	tool_call_id VARCHAR(100), 
	action VARCHAR(16) NOT NULL, 
	params_json TEXT NOT NULL, 
	status VARCHAR(12) NOT NULL, 
	result_json TEXT, 
	error_code VARCHAR(64), 
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	decided_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES ide_sessions (id) ON DELETE CASCADE
);
CREATE INDEX ix_ide_approvals_status_created ON ide_approvals (status, created_at);
CREATE INDEX ix_ide_approvals_session_id ON ide_approvals (session_id);
