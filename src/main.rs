//! Command-line entry point for the Kanboard agent worker.
//!
//! The binary can validate configuration, run the polling worker, or serve the
//! Kanboard MCP tools over stdio for an ACP agent subprocess.

use clap::{Parser, Subcommand};
use kanboard_agent_worker::{
    Result, config::load_config, kanboard_mcp::KanboardMcpServer, worker::Worker,
};
use rmcp::{ServiceExt, transport::stdio};
use tracing_subscriber::EnvFilter;

/// Parsed command-line options.
#[derive(Debug, Parser)]
#[command(name = "kanboard-agent-worker")]
struct Cli {
    /// Path to the YAML configuration file.
    #[arg(long, default_value = "config.yml")]
    config: String,
    /// Tracing filter directive, such as `INFO` or `kanboard_agent_worker=debug`.
    #[arg(long, default_value = "INFO")]
    log_level: String,
    /// Operation to run.
    #[command(subcommand)]
    command: Command,
}

/// Supported binary subcommands.
#[derive(Debug, Subcommand)]
enum Command {
    /// Validate configuration and Kanboard connectivity, then exit.
    Check,
    /// Start the polling worker loop.
    Run,
    /// Serve Kanboard tools as an MCP stdio server.
    Mcp,
}

/// Parse CLI options, initialize logging, and map application errors to exit codes.
#[tokio::main]
async fn main() -> std::process::ExitCode {
    let cli = Cli::parse();
    init_logging(&cli.log_level);
    match run(cli).await {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("error: {error}");
            std::process::ExitCode::from(1)
        }
    }
}

/// Execute the selected subcommand.
async fn run(cli: Cli) -> Result<()> {
    let Cli {
        config, command, ..
    } = cli;
    match command {
        Command::Check => {
            let config = load_config(&config)?;
            let worker = Worker::from_config(config).await?;
            for line in worker.check().await? {
                println!("{line}");
            }
            Ok(())
        }
        Command::Run => {
            let config = load_config(&config)?;
            let worker = Worker::from_config(config).await?;
            worker.run_forever().await
        }
        Command::Mcp => {
            let service = KanboardMcpServer::new()
                .serve(stdio())
                .await
                .map_err(|error| kanboard_agent_worker::AppError::Mcp(error.to_string()))?;
            service
                .waiting()
                .await
                .map_err(|error| kanboard_agent_worker::AppError::Mcp(error.to_string()))?;
            Ok(())
        }
    }
}

/// Initialize tracing once, falling back to `info` on invalid directives.
fn init_logging(level: &str) {
    let filter = EnvFilter::try_new(level).unwrap_or_else(|_| EnvFilter::new("info"));
    let _ = tracing_subscriber::fmt().with_env_filter(filter).try_init();
}
