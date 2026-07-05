import os
import sys
import argparse
import asyncio
import aiohttp
import aiofiles
from typing import Optional, List, Tuple

from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, TransferSpeedColumn

BASE_URL = "https://api.modrinth.com/v2"
console = Console()
__version__ = "v1.0.0"
__author__ = "红蓝灯（RBL）"

async def search_project(session: aiohttp.ClientSession, query: str) -> List[dict]:
    url = f"{BASE_URL}/search"
    params = {"query": query, "limit": 5}
    try:
        async with session.get(url, params=params, timeout=10) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("hits", [])
    except Exception as e:
        console.print(f"[red]搜索 '{query}' 失败: {e}[/red]")
        return []


async def get_project_versions(
    session: aiohttp.ClientSession,
    project_id: str,
    game_version: str,
    loader: str,
) -> List[dict]:
    url = f"{BASE_URL}/project/{project_id}/version"
    try:
        async with session.get(url, timeout=15) as resp:
            resp.raise_for_status()
            versions = await resp.json()
            filtered = []
            for v in versions:
                if game_version not in v.get("game_versions", []):
                    continue
                if loader and loader not in v.get("loaders", []):
                    continue
                filtered.append(v)
            return filtered
    except Exception as e:
        console.print(f"[red]获取版本列表失败: {e}[/red]")
        return []


def get_stable_version(versions: List[dict]) -> Optional[dict]:
    stable = []
    for v in versions:
        ver = v.get("version_number", "").lower()
        if any(k in ver for k in ("-beta", "-alpha", "-snapshot", "beta.", "alpha.")):
            continue
        stable.append(v)
    if not stable:
        return versions[0] if versions else None
    stable.sort(key=lambda x: x.get("date_published", ""), reverse=True)
    return stable[0]


def read_mcmds_file(file_path: str) -> Tuple[str, str, str, List[str]]:
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")

    with open(file_path, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f if line.strip()]

    if len(lines) < 5:
        raise ValueError("格式错误: 至少需要 版本, 目录, 加载器, ---, 和至少一个模组名")

    mc_version = lines[0]
    loader = lines[1]
    save_dir = lines[2]
    if not loader:
        raise ValueError("格式错误：加载器不能为空")
    if lines[3] != "---":
        raise ValueError("格式错误：第四行必须是 '---' 分隔符")
    mod_list = lines[4:]

    if not os.path.isabs(save_dir):
        save_dir = os.path.abspath(save_dir)

    return mc_version, save_dir, loader, mod_list


async def download_file(
    session: aiohttp.ClientSession,
    url: str,
    save_path: str,
    progress: Progress,
    task_id: int,
) -> bool:
    try:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        async with session.get(url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get('content-length', 0))
            downloaded = 0
            chunk_size = 8192

            async with aiofiles.open(save_path, 'wb') as f:
                async for chunk in resp.content.iter_chunked(chunk_size):
                    if chunk:
                        await f.write(chunk)
                        downloaded += len(chunk)
                        progress.update(task_id, completed=downloaded, total=total)
            progress.update(task_id, completed=total, total=total)
            return True
    except Exception as e:
        console.print(f"[red]下载失败 {os.path.basename(save_path)}: {e}[/red]")
        return False


async def download_mod(
    session: aiohttp.ClientSession,
    mod_name: str,
    mc_version: str,
    save_dir: str,
    loader: str,
    progress: Progress,
    task_id: int,
) -> Tuple[bool, str, Optional[str], Optional[str]]:
    progress.update(task_id, description=f"[cyan]搜索 {mod_name}[/cyan]")

    projects = await search_project(session, mod_name)
    if not projects:
        progress.update(task_id, description=f"[red]未找到 {mod_name}[/red]")
        return False, mod_name, None, None

    selected = projects[0]
    project_id = selected.get("project_id")
    title = selected.get("title", mod_name)

    progress.update(task_id, description=f"[cyan]获取版本 {title}[/cyan]")
    versions = await get_project_versions(session, project_id, mc_version, loader)
    if not versions:
        progress.update(task_id, description=f"[red]无适用版本 {title}[/red]")
        return False, title, None, None

    version = get_stable_version(versions)
    if not version:
        progress.update(task_id, description=f"[red]无稳定版 {title}[/red]")
        return False, title, None, None

    version_number = version.get("version_number", "unknown")
    files = version.get("files", [])
    if not files:
        progress.update(task_id, description=f"[red]无文件 {title}[/red]")
        return False, title, version_number, None

    jar_file = None
    for f in files:
        if f.get("primary") and f.get("filename", "").endswith(".jar"):
            jar_file = f
            break
    if not jar_file:
        jar_file = files[0]

    download_url = jar_file.get("url")
    filename = jar_file.get("filename", f"{mod_name}-{version_number}.jar")
    if not download_url:
        progress.update(task_id, description=f"[red]无下载链接 {title}[/red]")
        return False, title, version_number, None

    save_path = os.path.join(save_dir, filename)
    progress.update(task_id, description=f"[yellow]下载 {filename}[/yellow]")

    success = await download_file(session, download_url, save_path, progress, task_id)
    if success:
        progress.update(task_id, description=f"[green]✅ {title} {version_number}[/green]")
        return True, title, version_number, save_path
    else:
        progress.update(task_id, description=f"[red]❌ {title} 下载失败[/red]")
        return False, title, version_number, None


async def batch_download(
    mod_list: List[str],
    mc_version: str,
    save_dir: str,
    loader: str,
    concurrency: int,
):
    console.rule("[bold blue]MCModDownloader 批量下载[/bold blue]")
    console.print(f"程序版本:   [cyan]{__version__}[/cyan]")
    console.print(f"程序作者:   [cyan]{__author__}[/cyan]")
    console.print(f"MC 版本:   [cyan]{mc_version}[/cyan]")
    console.print(f"加载器:    [cyan]{loader}[/cyan]")
    console.print(f"模组数:    [cyan]{len(mod_list)}[/cyan]")
    console.print(f"并发数:    [cyan]{concurrency}[/cyan]")
    console.print(f"保存目录:   [cyan]{save_dir}[/cyan]")

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        semaphore = asyncio.Semaphore(concurrency)

        async with aiohttp.ClientSession() as session:
            tasks = []
            for mod in mod_list:
                task_id = progress.add_task(
                    description=f"[white]等待 {mod}[/white]",
                    total=None,
                    start=False,
                )
                progress.start_task(task_id)

                async def wrapped(mod=mod, tid=task_id):
                    async with semaphore:
                        return await download_mod(
                            session, mod, mc_version, save_dir, loader, progress, tid
                        )

                tasks.append(wrapped())

            results = await asyncio.gather(*tasks, return_exceptions=True)

        success_count = 0
        for res in results:
            if isinstance(res, Exception):
                console.print(f"[red]任务异常: {res}[/red]")
            elif res[0]:
                success_count += 1

    console.rule("[bold green]完成[/bold green]")
    console.print(f"✅ 成功: [green]{success_count}[/green] / 总数: {len(mod_list)} （成功率：[green]{success_count / len(mod_list) * 100:.2f}%[/green]）")


async def single_download(mod_name: str, mc_version: str, save_dir: str, loader: str):
    console.rule("[bold blue]MCModDownloader 单模组下载[/bold blue]")
    console.print(f"程序版本:   [cyan]{__version__}[/cyan]")
    console.print(f"程序作者:   [cyan]{__author__}[/cyan]")
    console.print(f"MC 版本:   [cyan]{mc_version}[/cyan]")
    console.print(f"加载器:     [cyan]{loader}[/cyan]")
    console.print(f"模组:      [cyan]{mod_name}[/cyan]")
    console.print(f"保存目录:   [cyan]{save_dir}[/cyan]")

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task(
            description=f"[white]准备下载 {mod_name}[/white]",
            total=None,
            start=True,
        )
        async with aiohttp.ClientSession() as session:
            ok, title, ver, path = await download_mod(
                session, mod_name, mc_version, save_dir, loader, progress, task_id
            )

    if ok:
        console.print(f"[green]✅ {title} ({ver}) 下载成功 → {path}[/green]")
    else:
        console.print(f"[red]❌ 下载失败: {title}[/red]")


def main():
    parser = argparse.ArgumentParser(description="MCMod Downloader")
    parser.add_argument("args", nargs="*", help="位置参数: <模组名> <MC版本> <模组加载器> [保存目录]")
    parser.add_argument("-f", "--file", help="批量文件 (.mcmds)")
    parser.add_argument("-c", "--concurrency", type=int, default=4, help="并发数 (默认 4)")

    args = parser.parse_args()

    if args.file:
        try:
            mc_version, save_dir, loader, mod_list = read_mcmds_file(args.file)
        except Exception as e:
            console.print(f"[red]读取文件失败: {e}[/red]")
            sys.exit(1)

        if not mod_list:
            console.print("[red]模组列表为空[/red]")
            sys.exit(1)

        asyncio.run(batch_download(mod_list, mc_version, save_dir, loader, args.concurrency))
    else:
        if len(args.args) < 3:
            console.print("[red]单模组模式需要提供: 模组名 MC版本 模组加载器 [保存目录][/red]")
            console.print("示例: mcmder sodium 1.20.1 fabric ./mods")
            sys.exit(1)

        mod_name = args.args[0]
        mc_version = args.args[1]
        loader = args.args[2]
        save_dir = args.args[3] if len(args.args) > 3 else "."

        asyncio.run(single_download(mod_name, mc_version, save_dir, loader))


if __name__ == "__main__":
    main()
