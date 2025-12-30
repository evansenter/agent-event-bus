//! Manual crop adjustment - trim bottom and right

use image::imageops::FilterType;
use image::ImageFormat;
use std::io::Cursor;
use std::path::PathBuf;

fn get_assets_dir() -> PathBuf {
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    PathBuf::from(manifest_dir)
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("assets")
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let assets_dir = get_assets_dir();
    let icon_path = assets_dir.join("icon.png");

    let img = image::open(&icon_path)?;
    println!("Current size: {}x{}", img.width(), img.height());

    // Trim 5% from bottom and right (keeping top-left anchor)
    let trim_percent = 0.08;
    let trim_pixels = (img.width() as f32 * trim_percent) as u32;
    let new_size = img.width() - trim_pixels;
    
    println!("Trimming {}px, new size: {}x{}", trim_pixels, new_size, new_size);
    
    let cropped = img.crop_imm(0, 0, new_size, new_size);

    // Save at different sizes
    for target_size in [512u32, 1024u32] {
        let resized = cropped.resize(target_size, target_size, FilterType::Lanczos3);
        let path = assets_dir.join(format!("icon-{}.png", target_size));
        let mut buf = Cursor::new(Vec::new());
        resized.write_to(&mut buf, ImageFormat::Png)?;
        std::fs::write(&path, buf.into_inner())?;
        println!("Saved: {}", path.display());
    }

    let path = assets_dir.join("icon.png");
    let mut buf = Cursor::new(Vec::new());
    cropped.write_to(&mut buf, ImageFormat::Png)?;
    std::fs::write(&path, buf.into_inner())?;
    println!("Saved: {}", path.display());

    Ok(())
}
