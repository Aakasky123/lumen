import { useEffect, useRef } from "react";
import { Animated, Easing, View } from "react-native";
import Svg, { Rect } from "react-native-svg";
import { alpha, useTheme } from "@/theme";

/**
 * The Lumen mark: four upright slabs, the inner pair set slightly lower (see
 * scripts/generate-icons.py). `working` sets the slabs shifting in sequence, the one motion that
 * means "Lumen is doing something" everywhere in the product.
 */
const SLABS = [
  { x: 3.6, y: 4.2, shade: false },
  { x: 8.3, y: 5.1, shade: true },
  { x: 13, y: 5.1, shade: true },
  { x: 17.7, y: 4.2, shade: false }
];

function Slab({ x, y, color, size, phase, working }: { x: number; y: number; color: string; size: number; phase: number; working: boolean }) {
  const shift = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    if (!working) {
      shift.setValue(0);
      return;
    }
    const loop = Animated.loop(
      Animated.sequence([
        Animated.delay(phase * 110),
        Animated.timing(shift, { toValue: 1, duration: 420, easing: Easing.inOut(Easing.quad), useNativeDriver: true }),
        Animated.timing(shift, { toValue: 0, duration: 420, easing: Easing.inOut(Easing.quad), useNativeDriver: true }),
        Animated.delay((3 - phase) * 110 + 240)
      ])
    );
    loop.start();
    return () => loop.stop();
  }, [phase, shift, working]);
  const unit = size / 24;
  return (
    <Animated.View
      style={{
        position: "absolute",
        left: x * unit,
        top: y * unit,
        transform: [{ translateY: shift.interpolate({ inputRange: [0, 1], outputRange: [0, -1.6 * unit] }) }]
      }}
    >
      <Svg width={2.7 * unit} height={14.7 * unit} viewBox="0 0 2.7 14.7">
        <Rect x={0} y={0} width={2.7} height={14.7} rx={0.75} fill={color} />
      </Svg>
    </Animated.View>
  );
}

export function LumenMark({ size = 20, working = false, tile = false }: { size?: number; working?: boolean; tile?: boolean }) {
  const { c } = useTheme();
  return (
    <View style={{ width: size, height: size }}>
      {tile ? (
        <Svg width={size} height={size} viewBox="0 0 24 24" style={{ position: "absolute" }}>
          <Rect x={0.5} y={0.5} width={23} height={23} rx={5.6} fill="#0d1f1a" stroke="#1c3a31" />
        </Svg>
      ) : null}
      {SLABS.map((slab, index) => (
        <Slab
          key={slab.x}
          x={slab.x}
          y={slab.y}
          size={size}
          phase={index}
          working={working}
          color={tile ? (slab.shade ? "#3fbf99" : "#5ee6bd") : slab.shade ? alpha(c.brand, 0.78) : c.brand}
        />
      ))}
    </View>
  );
}
