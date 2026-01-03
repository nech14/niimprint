import asyncio
from bleak import BleakClient

async def check_bluetooth_connection(device_address):
    """
    Checks if a Bluetooth Low Energy (BLE) device is currently connected.

    Args:
        device_address (str): The MAC address or UUID of the BLE device.
    """
    try:
        async with BleakClient(device_address) as client:
            if client.is_connected:
                print(f"Successfully connected to {device_address}.")
                # You can perform further operations with the connected device here
            else:
                print(f"Failed to connect to {device_address}.")
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    # Replace with the actual address of your Bluetooth device
    target_device_address = "XX:XX:XX:XX:XX:XX"  # Example: "A1:B2:C3:D4:E5:F6" or a UUID
    asyncio.run(check_bluetooth_connection(target_device_address))
