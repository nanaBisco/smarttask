from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
import base64

# Generate private key
private_key = ec.generate_private_key(ec.SECP256R1())
public_key = private_key.public_key()

# Convert keys to base64 (browser format)
def encode_public(key):
    return base64.urlsafe_b64encode(
        key.public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint
        )
    ).decode()

def encode_private(key):
    return base64.urlsafe_b64encode(
        key.private_numbers().private_value.to_bytes(32, 'big')
    ).decode()

print("\n=== SAVE THESE KEYS ===")
print("VAPID_PUBLIC_KEY =", encode_public(public_key))
print("VAPID_PRIVATE_KEY =", encode_private(private_key))