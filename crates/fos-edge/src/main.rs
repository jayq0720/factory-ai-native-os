//! fos-edge：厂端 daemon。W1 只有配置校验；采集、缓存、上行在 fos-edge v0.1 实现。
//!
//! 用法：fos-edge --check <edge.toml>

mod config;

use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    match args.as_slice() {
        [_, flag, path] if flag == "--check" => {
            let text = match std::fs::read_to_string(path) {
                Ok(t) => t,
                Err(e) => {
                    eprintln!("读取 {path} 失败：{e}");
                    return ExitCode::FAILURE;
                }
            };
            match config::EdgeConfig::parse(&text) {
                Ok(c) => {
                    println!(
                        "配置有效：tenant={} edge={} 点位 {} 个，缓存 {} 天（协议 v{}）",
                        c.tenant_id,
                        c.edge_id,
                        c.points.len(),
                        c.buffer_days,
                        fos_protocol::PROTOCOL_VERSION
                    );
                    for p in &c.points {
                        println!(
                            "  {:<24} {:?} / {:?} / {} / 每 {} 秒",
                            p.id,
                            p.driver,
                            p.source_path(),
                            p.unit,
                            p.interval_s
                        );
                    }
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("{e}");
                    ExitCode::FAILURE
                }
            }
        }
        _ => {
            eprintln!(
                "fos-edge {}\n用法：fos-edge --check <edge.toml>",
                env!("CARGO_PKG_VERSION")
            );
            ExitCode::from(2)
        }
    }
}
