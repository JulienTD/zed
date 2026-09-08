use anyhow::{Context as _, Result, bail};
use collections::HashMap;
use gpui::{App, Entity, Task};
use project::{AgentId, Project};
use remote::Interactive;
use serde::Deserialize;
use serde_json::{Value, json};
use std::time::Duration;
use util::command::new_command;

#[derive(Deserialize)]
pub struct WriterInspection {
    pub host: String,
    pub user: String,
    pub owner: Option<Value>,
    pub reason: Option<String>,
}

impl WriterInspection {
    pub fn description(&self) -> String {
        let owner = self
            .owner
            .as_ref()
            .map(|owner| {
                format!(
                    "{} (PID {}, {})",
                    owner["user"].as_str().unwrap_or("unknown"),
                    owner["pid"],
                    owner["executable"].as_str().unwrap_or("unknown")
                )
            })
            .unwrap_or_else(|| "not verified".into());
        let mut description = format!(
            "Host: {}\nAccount: {}\nOwner: {}",
            self.host, self.user, owner
        );
        if let Some(reason) = &self.reason {
            description.push_str(&format!("\n\n{reason}"));
        } else {
            description.push_str("\n\nStopping this process will interrupt its active task. Zed will then resume the saved conversation here.");
        }
        description
    }
}

pub fn inspect_writer(
    project: Entity<Project>,
    session_id: String,
    cx: &mut App,
) -> Task<Result<WriterInspection>> {
    run(
        project,
        json!({"action": "inspect", "session_id": session_id}),
        cx,
    )
}

pub fn stop_writer(
    project: Entity<Project>,
    session_id: String,
    owner: Value,
    cx: &mut App,
) -> Task<Result<WriterInspection>> {
    run(
        project,
        json!({"action": "stop", "session_id": session_id, "owner": owner}),
        cx,
    )
}

fn run(project: Entity<Project>, request: Value, cx: &mut App) -> Task<Result<WriterInspection>> {
    let store = project.read(cx).agent_server_store().clone();
    cx.spawn(async move |cx| {
        // Use the agent's resolved environment, including a configured CODEX_HOME.
        let command = store.update(cx, |store, cx| {
            let agent = store.get_external_agent(&AgentId::from(crate::CODEX_ID))
                .context("Codex is not registered. Reconnect the project and retry.")?;
            anyhow::Ok(agent.get_command(Vec::new(), HashMap::default(), &mut cx.to_async()))
        })?.await?;
        let args = vec!["-c".to_owned(), include_str!("codex_writer.py").to_owned(), request.to_string()];
        let env = command.env.unwrap_or_default();
        let mut process = project.read_with(cx, |project, cx| {
            if let Some(remote) = project.remote_client() {
                let template = remote.read(cx).build_command(Some("python3".into()), &args, &env,
                    project.default_path_list(cx).ordered_paths().next().map(|path| path.display().to_string()),
                    None, Interactive::No)?;
                let mut process = new_command(template.program);
                process.args(template.args).envs(template.env);
                Ok(process)
            } else if project.is_local() {
                let mut process = new_command("python3");
                process.args(args).envs(env);
                Ok(process)
            } else {
                bail!("The project's remote host is unavailable. Reconnect it before resolving this session.")
            }
        })?;
        process.kill_on_drop(true);
        let output = cx.background_executor().spawn(async move { process.output().await });
        let timer = cx.background_executor().timer(Duration::from_secs(20));
        let output = match futures::future::select(output, timer).await {
            futures::future::Either::Left((output, _)) => output.context("Could not inspect Codex's writer; python3 is required on the agent host")?,
            futures::future::Either::Right(_) => bail!("Timed out contacting the Codex host. Reconnect the project and retry."),
        };
        if !output.status.success() {
            bail!("{}", String::from_utf8_lossy(&output.stderr).trim());
        }
        serde_json::from_slice(&output.stdout).context("Could not read the Codex writer owner")
    })
}
