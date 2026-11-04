import std;
import os;
struct Candle { start: i64, end: i64 }
fn main() {
    var path: u8[>..] = [];
    guard arg_count() == 2 else { print("expected input path"); exit(2); }
    arg(1, path);
    var text: u8[>..] = [];
    guard read_file(path, text) else { print("cannot read input"); exit(1); }
    var candles: Candle[>..] = [];
    each_split(text, '\n') {
        guard it.len > 0;
        let fields = split(it, ',');
        assert(fields.len == 2);
        let start, ok1 = parse_int(fields[0]);
        let end, ok2 = parse_int(fields[1]);
        assert(ok1 && ok2);
        candles.push(Candle { start: start, end: end });
    };
    for n in 1..candles.len + 1 {
        var continuous = true;
        for i in 1..n {
            if candles[i-1].end != candles[i].start { continuous = false; break; }
        }
        if continuous { print("1"); } else { print("0"); }
    }
}
