//! Remove grey background, make it transparent

use image::{ImageFormat, Rgba, RgbaImage};
use image::imageops::FilterType;
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

    let img = image::open(&icon_path)?.to_rgba8();
    println!("Size: {}x{}", img.width(), img.height());

    // Sample corner pixels to detect background color
    let corner = img.get_pixel(5, 5);
    println!("Corner pixel (likely bg): {:?}", corner);

    let mut output = RgbaImage::new(img.width(), img.height());
    
    // Threshold for "close to background color"
    let threshold = 30u8;
    
    for (x, y, pixel) in img.enumerate_pixels() {
        let diff_r = (pixel[0] as i16 - corner[0] as i16).unsigned_abs() as u8;
        let diff_g = (pixel[1] as i16 - corner[1] as i16).unsigned_abs() as u8;
        let diff_b = (pixel[2] as i16 - corner[2] as i16).unsigned_abs() as u8;
        
        if diff_r < threshold && diff_g < threshold && diff_b < threshold {
            // Make transparent
            output.put_pixel(x, y, Rgba([0, 0, 0, 0]));
        } else {
            output.put_pixel(x, y, *pixel);
        }
    }

    // Save
    for target_size in [512u32, 1024u32] {
        let resized = image::imageops::resize(&output, target_size, target_size, FilterType::Lanczos3);
        let path = assets_dir.join(format!("icon-{}.png", target_size));
        let mut buf = Cursor::new(Vec::new());
        resized.write_to(&mut buf, ImageFormat::Png)?;
        std::fs::write(&path, buf.into_inner())?;
        println!("Saved: {}", path.display());
    }

    let path = assets_dir.join("icon.png");
    let mut buf = Cursor::new(Vec::new());
    output.write_to(&mut buf, ImageFormat::Png)?;
    std::fs::write(&path, buf.into_inner())?;
    println!("Saved: {}", path.display());

    Ok(())
}
